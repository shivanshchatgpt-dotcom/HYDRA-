"""Outbound web search + fetch (v0.5), stdlib only.

Two capabilities, both requested explicitly by the caller — the
daemon never browses on its own:

* ``search(query, count)`` — DuckDuckGo HTML endpoint (no API key,
  no account, no cookie). Results are parsed from the HTML page.
  The backend can rate-limit or return an empty page; that is
  reported honestly as an error, never as "no results found
  anywhere".
* ``fetch(url, max_bytes)`` — one HTTPS/HTTP GET, capped in bytes,
  HTML reduced to text.

Hard limits, by design:

* **SSRF guard.** Only ``http:``/``https:``; the host must resolve
  to a public IP. Loopback, RFC1918, link-local, CGNAT
  (100.64.0.0/10 — Tailscale's range), multicast, reserved and
  unspecified addresses are refused *before* the socket opens.
  Every redirect hop is re-validated, so an open redirect cannot
  bounce the daemon to 127.0.0.1 or a Tailscale peer.
* **No credentials, no cookies.** Proxy settings and the user's
  browser session are not used. ``user:pass@`` URLs are refused
  rather than sent.
* **Caps.** Redirects are followed at most 5 times; bodies are
  capped in bytes (default 200KB, caller may lower, never raise
  above ``MAX_BYTES``).
* **Audit redaction.** The daemon logs host + URL hash + sizes
  only — never the URL path/query or the page text.
"""

from __future__ import annotations

import hashlib
import html
import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

DEFAULT_TIMEOUT = 20.0
DEFAULT_MAX_BYTES = 200_000
MAX_BYTES = 500_000
MAX_REDIRECTS = 5
MAX_COUNT = 10
_UA = "HYDRA/0.5 (local agent; +https://github.com/shivanshchatgpt-dotcom/HYDRA-)"
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_NAT64 = (ipaddress.ip_network("64:ff9b::/96"),
          ipaddress.ip_network("64:ff9b:1::/48"))


class WebError(RuntimeError):
    """Base class for web search/fetch failures."""


class WebBlockedError(WebError):
    """The URL was refused by policy before any connection (SSRF guard)."""


class WebFetchError(WebError):
    """The request was made and failed, or the response was unusable."""


# ---------------------------------------------------------------- URL policy

def _ip_blocked(ip: ipaddress._BaseAddress) -> bool:
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif any(ip in net for net in _NAT64):
            # NAT64 reaches public IPv4 (common on IPv6-only networks),
            # but can also embed private targets: judge the embedded IPv4.
            ip = ipaddress.IPv4Address(ip.packed[-4:])
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_multicast or ip.is_reserved or ip.is_unspecified
        or (ip.version == 4 and ip in _CGNAT)
    )


def _resolve(host: str, port: int) -> list[ipaddress._BaseAddress]:
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise WebFetchError(f"cannot resolve host: {exc}") from exc
    addrs = []
    for info in infos:
        try:
            addrs.append(ipaddress.ip_address(info[4][0].split("%")[0]))
        except ValueError:
            continue
    if not addrs:
        raise WebFetchError("host resolved to no usable address")
    return addrs


def validate_url(url: str) -> str:
    """Return the URL unchanged if it is safe to request, else raise."""
    if not isinstance(url, str) or not url.strip():
        raise WebBlockedError("url must be a non-empty string")
    if len(url) > 8192:
        raise WebBlockedError("url too long")
    parts = urllib.parse.urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise WebBlockedError(
            f"scheme not allowed: {parts.scheme or '(none)'} — http/https only")
    if not parts.hostname:
        raise WebBlockedError("url has no host")
    if parts.username or parts.password:
        raise WebBlockedError(
            "credentials in URLs are refused (they would be sent in clear)")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise WebBlockedError(f"bad port: {exc}") from exc
    for ip in _resolve(parts.hostname, port):
        if _ip_blocked(ip):
            raise WebBlockedError(
                f"refused: {parts.hostname} resolves to a non-public address "
                f"({ip}); loopback/LAN/Tailscale targets are not fetchable")
    return url.strip()


def text_fingerprint(text: str) -> dict:
    """Audit-safe description of free text (query): hash + length only."""
    return {
        "sha256_12": hashlib.sha256(text.encode()).hexdigest()[:12],
        "len": len(text),
    }


def url_fingerprint(url: str) -> dict:
    """Audit-safe description of a URL: host + short hash only."""
    parts = urllib.parse.urlsplit(url)
    return {
        "host": parts.hostname or "",
        "url_sha256_12": hashlib.sha256(url.encode()).hexdigest()[:12],
    }


# ---------------------------------------------------------------- HTML->text

_SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}
_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4",
               "h5", "h6", "section", "article", "header", "footer",
               "blockquote", "pre", "table", "ul", "ol", "hr"}
_WS = re.compile(r"[ \t\r\f\v]+")
_NL = re.compile(r"\n\s*\n+")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> tuple[str, str]:
    """Strip markup to readable text. Returns (title, text)."""
    parser = _TextExtractor()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # malformed HTML: fall back to tags-off
        text = re.sub(r"<[^>]+>", " ", markup)
        return "", _NL.sub("\n\n", _WS.sub(" ", html.unescape(text))).strip()
    text = _WS.sub(" ", "".join(parser.parts).replace("\xa0", " "))
    text = "\n".join(line.strip() for line in text.splitlines())
    return " ".join(parser.title.split()), _NL.sub("\n\n", text).strip()


# ---------------------------------------------------------------- fetch

class _SafeRedirects(urllib.request.HTTPRedirectHandler):
    # Re-validate every hop: an open redirect must not reach 127.0.0.1.
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, max_bytes: int = DEFAULT_MAX_BYTES,
          timeout: float = DEFAULT_TIMEOUT, *,
          _allow_private: bool = False) -> dict:
    """One GET, capped, HTML reduced to text.

    ``_allow_private`` exists only for tests (local HTTP server); it is
    never exposed by the daemon or the MCP server.
    """
    url = str(url or "").strip()
    if not _allow_private:
        validate_url(url)
    elif urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise WebBlockedError("http/https only")
    try:
        cap = int(max_bytes)
    except (TypeError, ValueError):
        cap = DEFAULT_MAX_BYTES
    cap = max(1, min(cap, MAX_BYTES))

    opener = urllib.request.build_opener(_SafeRedirects())
    req = urllib.request.Request(url, method="GET", headers={
        "User-Agent": _UA,
        "Accept": "text/html,application/json;q=0.9,text/plain;q=0.8,*/*;q=0.5",
        "Accept-Language": "en;q=0.9",
    })
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read(cap + 1)
            final_url = resp.geturl()
            status = resp.status
            ctype = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        raise WebFetchError(f"HTTP {exc.code} from {url_fingerprint(url)['host']}") from exc
    except urllib.error.URLError as exc:
        raise WebFetchError(f"fetch failed: {exc.reason}") from exc
    except (OSError, ValueError) as exc:
        raise WebFetchError(f"fetch failed: {exc}") from exc

    truncated = len(raw) > cap
    raw = raw[:cap]
    charset = "utf-8"
    match = re.search(r"charset=([\w.-]+)", ctype, re.I)
    if match:
        charset = match.group(1)
    try:
        body = raw.decode(charset, errors="replace")
    except LookupError:
        body = raw.decode("utf-8", errors="replace")

    is_html = "html" in ctype.lower() or body.lstrip()[:15].lower().startswith("<!doctype")
    if is_html:
        title, text = html_to_text(body)
    else:
        title, text = "", body
    return {
        "url": final_url,
        "status": status,
        "content_type": ctype,
        "bytes": len(raw),
        "truncated": truncated,
        "title": title,
        "text": text,
    }


# ---------------------------------------------------------------- search

_DDG_RESULT = re.compile(
    r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_DDG_SNIPPET = re.compile(
    r'<(?:a|div)[^>]+class="result__snippet"[^>]*>(.*?)</(?:a|div)>', re.S)
_TAGS = re.compile(r"<[^>]+>")


def _clean(fragment: str) -> str:
    return " ".join(html.unescape(_TAGS.sub(" ", fragment)).split())


def _unwrap_ddg(href: str) -> str:
    href = html.unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    parts = urllib.parse.urlsplit(href)
    if parts.hostname and parts.hostname.endswith("duckduckgo.com"):
        query = urllib.parse.parse_qs(parts.query)
        if query.get("uddg"):
            return query["uddg"][0]
    return href


def parse_ddg_html(markup: str) -> list[dict]:
    """Parse DuckDuckGo HTML results. Kept separate so it is testable."""
    titles = _DDG_RESULT.findall(markup)
    snippets = [_clean(s) for s in _DDG_SNIPPET.findall(markup)]
    results = []
    for i, (href, title_html) in enumerate(titles):
        url = _unwrap_ddg(href)
        if not url.startswith(("http://", "https://")):
            continue
        results.append({
            "title": _clean(title_html),
            "url": url,
            "snippet": snippets[i] if i < len(snippets) else "",
        })
    return results


def search(query: str, count: int = 5,
           timeout: float = DEFAULT_TIMEOUT) -> list[dict]:
    """DuckDuckGo HTML search (no key, no cookies). Raises on failure."""
    query = str(query or "").strip()
    if not query:
        raise WebError("query must be a non-empty string")
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 5
    count = max(1, min(count, MAX_COUNT))

    data = urllib.parse.urlencode({"q": query, "kl": "wt-wt"}).encode()
    req = urllib.request.Request(
        "https://html.duckduckgo.com/html/", data=data, method="POST",
        headers={
            "User-Agent": _UA,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html",
            "Accept-Language": "en;q=0.9",
        })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(MAX_BYTES).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise WebFetchError(
            f"search backend returned HTTP {exc.code} (rate-limited?); "
            "retry later or use web_fetch with a known URL") from exc
    except urllib.error.URLError as exc:
        raise WebFetchError(
            f"search backend unreachable: {exc.reason}") from exc
    except (OSError, ValueError) as exc:
        raise WebFetchError(f"search backend error: {exc}") from exc

    results = parse_ddg_html(body)
    if not results:
        raise WebFetchError(
            "search backend returned no parsable results (layout change, "
            "rate limit, or genuinely zero hits); use web_fetch instead")
    return results[:count]
