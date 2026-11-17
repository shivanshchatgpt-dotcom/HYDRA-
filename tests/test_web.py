"""Tests for hydra/web.py (v0.5 web search + fetch), stdlib unittest.

Run from the repo root:
    python3 -m unittest tests.test_web -v

No internet needed: the SSRF guard is tested against literal IPs and
``localhost`` (resolved locally), the HTML/DDG parsers against
fixtures, and fetch against a throwaway loopback HTTP server using the
tests-only ``_allow_private`` flag. Real DuckDuckGo and real site
fetches stay on the manual checklist in docs/MCP_SETUP.md.
"""

from __future__ import annotations

import http.server
import ipaddress
import threading
import unittest

from hydra import web


class UrlPolicyTests(unittest.TestCase):
    def test_blocked_ips(self):
        for raw in ("127.0.0.1", "127.1.2.3", "10.0.0.5", "172.16.3.4",
                    "192.168.1.1", "169.254.169.254", "100.64.0.1",
                    "100.127.255.254", "0.0.0.0", "224.0.0.1",
                    "::1", "fe80::1", "::ffff:127.0.0.1"):
            self.assertTrue(web._ip_blocked(ipaddress.ip_address(raw)), raw)

    def test_public_ips_allowed(self):
        for raw in ("8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"):
            self.assertFalse(web._ip_blocked(ipaddress.ip_address(raw)), raw)

    def test_scheme_and_shape(self):
        for bad in ("file:///etc/passwd", "ftp://example.com/x", "gopher://x",
                    "javascript:alert(1)", "", "   ", "http://"):
            with self.assertRaises(web.WebBlockedError, msg=bad):
                web.validate_url(bad)

    def test_credentials_refused(self):
        with self.assertRaises(web.WebBlockedError):
            web.validate_url("https://user:pw@example.com/")

    def test_localhost_and_tailnet_refused(self):
        for bad in ("http://localhost:8765/health",
                    "http://127.0.0.1/", "http://100.74.209.52/",
                    "http://[::1]:9000/"):
            with self.assertRaises(web.WebBlockedError, msg=bad):
                web.validate_url(bad)

    def test_public_literal_ip_allowed(self):
        self.assertEqual(web.validate_url("https://8.8.8.8/"),
                         "https://8.8.8.8/")

    def test_fingerprint_has_no_path_or_query(self):
        fp = web.url_fingerprint("https://example.com/secret?token=abc")
        self.assertEqual(fp["host"], "example.com")
        self.assertEqual(len(fp["url_sha256_12"]), 12)
        self.assertNotIn("secret", str(fp))
        self.assertNotIn("token", str(fp))


class HtmlTests(unittest.TestCase):
    def test_title_text_entities_and_skips(self):
        title, text = web.html_to_text(
            "<html><head><title>Hi &amp; bye</title></head><body>"
            "<script>alert(1)</script><style>.x{}</style>"
            "<p>one</p><p>two&nbsp;three</p><div>four</div></body></html>")
        self.assertEqual(title, "Hi & bye")
        self.assertIn("one", text)
        self.assertIn("two three", text)
        self.assertNotIn("alert", text)
        self.assertNotIn(".x", text)

    def test_ddg_parse_unwraps_redirects(self):
        markup = (
            '<a rel="nofollow" class="result__a" '
            'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=x">'
            'Example <b>A</b></a>'
            '<a class="result__snippet">Snippet <b>one</b></a>'
            '<a rel="nofollow" class="result__a" href="https://b.example/">'
            'B</a>'
            '<div class="result__snippet">Snippet two</div>')
        results = web.parse_ddg_html(markup)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["url"], "https://example.com/a")
        self.assertEqual(results[0]["title"], "Example A")
        self.assertEqual(results[0]["snippet"], "Snippet one")
        self.assertEqual(results[1]["url"], "https://b.example/")
        self.assertEqual(results[1]["snippet"], "Snippet two")

    def test_ddg_parse_ignores_non_http(self):
        markup = '<a class="result__a" href="javascript:void(0)">x</a>'
        self.assertEqual(web.parse_ddg_html(markup), [])


class _Handler(http.server.BaseHTTPRequestHandler):
    redirect_to = None

    def do_GET(self):  # noqa: N802
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", self.redirect_to)
            self.end_headers()
            return
        body = b"<html><title>T</title><body><p>hello web</p></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class FetchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_private_blocked_without_test_flag(self):
        with self.assertRaises(web.WebBlockedError):
            web.fetch(f"http://127.0.0.1:{self.port}/")

    def test_fetch_text_and_truncation(self):
        out = web.fetch(f"http://127.0.0.1:{self.port}/",
                        _allow_private=True)
        self.assertEqual(out["status"], 200)
        self.assertIn("hello web", out["text"])
        self.assertFalse(out["truncated"])
        tiny = web.fetch(f"http://127.0.0.1:{self.port}/",
                         max_bytes=10, _allow_private=True)
        self.assertTrue(tiny["truncated"])
        self.assertLessEqual(tiny["bytes"], 10)

    def test_max_bytes_capped(self):
        out = web.fetch(f"http://127.0.0.1:{self.port}/",
                        max_bytes=10_000_000, _allow_private=True)
        self.assertLessEqual(out["bytes"], web.MAX_BYTES)

    def test_redirect_to_loopback_blocked(self):
        _Handler.redirect_to = f"http://127.0.0.1:{self.port}/x"
        try:
            with self.assertRaises(web.WebError):
                web.fetch(f"http://127.0.0.1:{self.port}/redirect",
                          _allow_private=True)
        finally:
            _Handler.redirect_to = None


if __name__ == "__main__":
    unittest.main()
