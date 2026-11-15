"""HYDRA v0.2 tests (stdlib unittest only).

Run from the repo root:
    python3 -m unittest discover -s tests -v

Covers every v0.2 endpoint: /edit, /read line ranges, /search,
background jobs, bubblewrap sandbox option, /memory/delete,
/memory/list, and the GUI endpoints' failure paths. GUI success
paths and real sandboxing need a live Wayland session + bwrap and
are verified on Baji's laptop, not here: tests that need those
tools skip when the tools are absent, and assert the clear error
when they are.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hydra.daemon import VERSION, make_server
from hydra.gui import (
    GuiInputError,
    GuiUnavailableError,
    input_click,
    input_move,
    input_type,
    take_screenshot,
)
from hydra.memory_store import MemoryStore
from hydra.sandbox import SandboxUnavailableError, build_bwrap_argv
from hydra.search import SearchError, run_search

TEST_TOKEN = "test-token-" + "x" * 24  # >= 20 chars, test-only


def _write_token_file(path: Path, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, (TEST_TOKEN + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, mode)


class HttpDaemonV02Tests(unittest.TestCase):
    """End-to-end tests against a real v0.2 daemon instance."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws = root / "ws"
        self.ws.mkdir()
        (self.ws / "hello.txt").write_text("hello hydra\nsecond line\n")
        (self.ws / "code.py").write_text(
            "def greet():\n    return 'TODO: fix me'\n# TODO later\n")
        (self.ws / "lines.txt").write_text("1\n2\n3\n4\n5\n")
        data_dir = root / "data"
        data_dir.mkdir()
        token_file = data_dir / "token"
        _write_token_file(token_file)
        self.config = SimpleNamespace(
            bind="127.0.0.1", port=0, workspace=self.ws,
            data_dir=data_dir, token_file=token_file,
            audit_path=data_dir / "audit.jsonl",
            db_path=data_dir / "memory.db",
            read_only_paths=[], extra_patterns=[],
            allow_systemctl_manage=False,
        )
        self.server = make_server(self.config, TEST_TOKEN)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        try:
            self.server.RequestHandlerClass.jobs.kill_all()
        except Exception:
            pass
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        try:
            self.server.RequestHandlerClass.store.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def request(self, method, path, payload=None, token="correct"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        hdrs = {}
        body = None
        if payload is not None:
            body = json.dumps(payload)
            hdrs["Content-Type"] = "application/json"
        if token == "correct":
            hdrs["Authorization"] = f"Bearer {TEST_TOKEN}"
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
        conn.close()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"_raw": raw}
        return resp.status, parsed

    def poll_until_done(self, job_id, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, body = self.request(
                "POST", "/jobs/poll", {"job_id": job_id})
            self.assertEqual(status, 200)
            if body["status"] != "running":
                return body
            time.sleep(0.1)
        self.fail(f"job {job_id} did not finish in {timeout}s")

    # -- version -------------------------------------------------------

    def test_health_reports_v02(self):
        status, body = self.request("GET", "/health", token="none")
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], "0.2.0")
        self.assertEqual(VERSION, "0.2.0")

    # -- /edit ---------------------------------------------------------

    def test_edit_unique_replace(self):
        status, body = self.request("POST", "/edit", {
            "path": "hello.txt", "old_string": "hello hydra",
            "new_string": "hello HYDRA v0.2"})
        self.assertEqual(status, 200)
        self.assertEqual(body["replaced"], 1)
        self.assertEqual(
            (self.ws / "hello.txt").read_text(),
            "hello HYDRA v0.2\nsecond line\n")

    def test_edit_dry_run_writes_nothing(self):
        status, body = self.request("POST", "/edit", {
            "path": "hello.txt", "old_string": "hello",
            "new_string": "bye", "dry_run": True})
        self.assertEqual(status, 200)
        self.assertTrue(body["dry_run"])
        self.assertEqual(body["would_replace"], 1)
        self.assertEqual((self.ws / "hello.txt").read_text(),
                         "hello hydra\nsecond line\n")

    def test_edit_zero_matches(self):
        status, body = self.request("POST", "/edit", {
            "path": "hello.txt", "old_string": "nope-not-there",
            "new_string": "x"})
        self.assertEqual(status, 400)
        self.assertIn("not found", body["error"])

    def test_edit_ambiguous_matches(self):
        (self.ws / "dup.txt").write_text("foo bar foo bar foo\n")
        status, body = self.request("POST", "/edit", {
            "path": "dup.txt", "old_string": "foo", "new_string": "baz"})
        self.assertEqual(status, 409)
        self.assertEqual(body["matches"], 3)
        # File unchanged after the refusal.
        self.assertEqual((self.ws / "dup.txt").read_text(),
                         "foo bar foo bar foo\n")

    def test_edit_replace_all(self):
        (self.ws / "dup.txt").write_text("foo bar foo bar foo\n")
        status, body = self.request("POST", "/edit", {
            "path": "dup.txt", "old_string": "foo", "new_string": "baz",
            "replace_all": True})
        self.assertEqual(status, 200)
        self.assertEqual(body["replaced"], 3)
        self.assertEqual((self.ws / "dup.txt").read_text(),
                         "baz bar baz bar baz\n")

    def test_edit_empty_old_string(self):
        status, _ = self.request("POST", "/edit", {
            "path": "hello.txt", "old_string": "", "new_string": "x"})
        self.assertEqual(status, 400)

    def test_edit_jail_and_missing(self):
        status, _ = self.request("POST", "/edit", {
            "path": "../escape.txt", "old_string": "a", "new_string": "b"})
        self.assertEqual(status, 403)
        status, _ = self.request("POST", "/edit", {
            "path": "missing.txt", "old_string": "a", "new_string": "b"})
        self.assertEqual(status, 404)

    def test_edit_binary_file_refused(self):
        (self.ws / "bin.dat").write_bytes(b"\x00\xff\xfe binary")
        status, body = self.request("POST", "/edit", {
            "path": "bin.dat", "old_string": "a", "new_string": "b"})
        self.assertEqual(status, 400)
        self.assertIn("UTF-8", body["error"])

    # -- /read line ranges ----------------------------------------------

    def test_read_line_range(self):
        status, body = self.request("POST", "/read", {
            "path": "lines.txt", "start_line": 2, "end_line": 4})
        self.assertEqual(status, 200)
        self.assertEqual(body["content"], "2\n3\n4\n")
        self.assertEqual(body["lines_returned"], 3)
        self.assertEqual(body["total_lines"], 5)
        self.assertEqual(body["start_line"], 2)
        self.assertEqual(body["end_line"], 4)

    def test_read_range_open_ended_and_past_eof(self):
        status, body = self.request("POST", "/read", {
            "path": "lines.txt", "start_line": 4})
        self.assertEqual(body["content"], "4\n5\n")
        status, body = self.request("POST", "/read", {
            "path": "lines.txt", "start_line": 99})
        self.assertEqual(status, 200)
        self.assertEqual(body["content"], "")
        self.assertEqual(body["lines_returned"], 0)

    def test_read_range_invalid(self):
        for payload in ({"start_line": 0}, {"start_line": 3, "end_line": 2},
                        {"start_line": "two"}):
            status, _ = self.request(
                "POST", "/read", {"path": "lines.txt", **payload})
            self.assertEqual(status, 400, f"should refuse {payload}")

    def test_read_without_range_unchanged(self):
        status, body = self.request("POST", "/read", {"path": "hello.txt"})
        self.assertEqual(status, 200)
        self.assertEqual(body["content"], "hello hydra\nsecond line\n")

    # -- /search ----------------------------------------------------------

    def test_search_finds_matches(self):
        status, body = self.request("POST", "/search", {"pattern": "TODO"})
        self.assertEqual(status, 200)
        self.assertIn(body["backend"], ("ripgrep", "grep"))
        self.assertEqual(body["match_count"], 2)
        paths = [m["path"] for m in body["matches"]]
        self.assertTrue(all(p.endswith("code.py") for p in paths))
        lines = sorted(m["line"] for m in body["matches"])
        self.assertEqual(lines, [2, 3])

    def test_search_glob_and_case(self):
        status, body = self.request("POST", "/search", {
            "pattern": "todo", "case_sensitive": False, "glob": "*.py"})
        self.assertEqual(status, 200)
        self.assertEqual(body["match_count"], 2)
        status, body = self.request("POST", "/search", {
            "pattern": "TODO", "glob": "*.txt"})
        self.assertEqual(status, 200)
        self.assertEqual(body["match_count"], 0)

    def test_search_fixed_strings_and_regex(self):
        status, body = self.request("POST", "/search", {
            "pattern": "TODO: fix", "fixed_strings": True})
        self.assertEqual(status, 200)
        self.assertEqual(body["match_count"], 1)
        status, body = self.request("POST", "/search", {
            "pattern": "TODO.*fix"})
        self.assertEqual(status, 200)
        self.assertEqual(body["match_count"], 1)

    def test_search_no_match_and_bad_pattern(self):
        status, body = self.request("POST", "/search", {"pattern": "zzzqqq"})
        self.assertEqual(status, 200)
        self.assertEqual(body["matches"], [])
        status, body = self.request("POST", "/search", {"pattern": "[invalid"})
        self.assertEqual(status, 400)

    def test_search_jail_and_bad_glob(self):
        status, _ = self.request("POST", "/search", {
            "pattern": "x", "path": "/etc"})
        self.assertEqual(status, 403)
        status, _ = self.request("POST", "/search", {
            "pattern": "x", "glob": "../*.py"})
        self.assertEqual(status, 400)

    def test_search_pattern_not_in_audit_log(self):
        secret_pattern = "sekrit-pattern-987654321"
        self.request("POST", "/search", {"pattern": secret_pattern})
        audit = self.config.audit_path.read_text(encoding="utf-8")
        self.assertNotIn(secret_pattern, audit)
        self.assertIn("/search", audit)

    # -- background jobs --------------------------------------------------

    def test_jobs_start_poll_output(self):
        status, body = self.request("POST", "/jobs/start", {
            "command": "echo job-output-123"})
        self.assertEqual(status, 200)
        job_id = body["job_id"]
        done = self.poll_until_done(job_id)
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["returncode"], 0)
        self.assertIn("job-output-123", done["stdout"])
        self.assertFalse(done["truncated"])

    def test_jobs_poll_offset(self):
        status, body = self.request("POST", "/jobs/start", {
            "command": "printf 'abcdefgh'"})
        job_id = body["job_id"]
        done = self.poll_until_done(job_id)
        total = done["stdout_total_len"]
        self.assertEqual(total, 8)
        status, body = self.request(
            "POST", "/jobs/poll", {"job_id": job_id, "offset": 4})
        self.assertEqual(body["stdout"], "efgh")
        self.assertEqual(body["stdout_offset"], 4)

    def test_jobs_kill(self):
        status, body = self.request("POST", "/jobs/start", {
            "command": "sleep 30"})
        job_id = body["job_id"]
        time.sleep(0.3)
        status, body = self.request("POST", "/jobs/kill", {"job_id": job_id})
        self.assertEqual(status, 200)
        self.assertTrue(body["killed"])
        status, body = self.request("POST", "/jobs/poll", {"job_id": job_id})
        self.assertEqual(body["status"], "killed")

    def test_jobs_unknown_and_blocked(self):
        status, _ = self.request("POST", "/jobs/poll", {"job_id": "nope"})
        self.assertEqual(status, 404)
        status, _ = self.request("POST", "/jobs/kill", {"job_id": "nope"})
        self.assertEqual(status, 404)
        status, body = self.request("POST", "/jobs/start", {
            "command": "sudo id"})
        self.assertEqual(status, 403)
        self.assertTrue(body.get("blocked"))

    def test_jobs_list(self):
        self.request("POST", "/jobs/start", {"command": "echo one"})
        status, body = self.request("POST", "/jobs/list", {})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(body["count"], 1)
        self.assertIn("job_id", body["jobs"][0])

    def test_jobs_timeout_kills(self):
        status, body = self.request("POST", "/jobs/start", {
            "command": "sleep 30", "timeout_seconds": 1})
        job_id = body["job_id"]
        done = self.poll_until_done(job_id, timeout=10)
        self.assertEqual(done["status"], "killed")
        self.assertTrue(done["timed_out"])

    # -- sandbox option -----------------------------------------------------

    def test_exec_sandbox_flag_reported(self):
        status, body = self.request("POST", "/exec", {"command": "echo hi"})
        self.assertEqual(status, 200)
        self.assertFalse(body["sandboxed"])

    @unittest.skipIf(shutil.which("bwrap") is not None,
                     "bwrap present: 501 path not exercised")
    def test_exec_sandbox_missing_bwrap(self):
        status, body = self.request("POST", "/exec", {
            "command": "echo hi", "sandbox": True})
        self.assertEqual(status, 501)
        self.assertIn("bubblewrap", body["error"])

    @unittest.skipUnless(shutil.which("bwrap") is not None,
                         "needs bwrap (live laptop check)")
    def test_exec_sandbox_runs(self):
        status, body = self.request("POST", "/exec", {
            "command": "pwd", "sandbox": True})
        self.assertEqual(status, 200)
        self.assertTrue(body["sandboxed"])
        self.assertIn(str(self.ws), body["stdout"])

    # -- memory delete / list -----------------------------------------------

    def test_memory_delete_and_list(self):
        _, b1 = self.request("POST", "/memory/add", {
            "text": "first memory alpha", "kind": "fact"})
        _, b2 = self.request("POST", "/memory/add", {
            "text": "second memory beta", "kind": "note"})
        status, body = self.request("POST", "/memory/list", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 2)
        self.assertEqual(body["memories"][0]["id"], b2["id"])  # newest first
        status, body = self.request(
            "POST", "/memory/list", {"kind": "fact"})
        self.assertEqual(body["count"], 1)
        status, body = self.request(
            "POST", "/memory/delete", {"id": b1["id"]})
        self.assertEqual(status, 200)
        self.assertTrue(body["deleted"])
        status, body = self.request(
            "POST", "/memory/search", {"query": "alpha"})
        self.assertEqual(body["results"], [])
        status, _ = self.request(
            "POST", "/memory/delete", {"id": b1["id"]})
        self.assertEqual(status, 404)

    def test_memory_delete_relation(self):
        _, rel = self.request("POST", "/memory/graph/add", {
            "subject": "Baji", "predicate": "uses", "object": "Arch"})
        status, body = self.request(
            "POST", "/memory/delete", {"relation_id": rel["id"]})
        self.assertEqual(status, 200)
        status, body = self.request(
            "POST", "/memory/graph/neighbors", {"entity": "Baji"})
        self.assertEqual(body["neighbors"], [])

    # -- GUI failure paths ----------------------------------------------------

    @unittest.skipIf(bool(os.environ.get("WAYLAND_DISPLAY")
                          or os.environ.get("DISPLAY")),
                     "graphical session present: failure path not exercised")
    def test_screen_shot_no_session(self):
        status, body = self.request("POST", "/screen/shot", {})
        self.assertEqual(status, 501)
        self.assertIn("WAYLAND_DISPLAY", body["error"])

    @unittest.skipUnless(shutil.which("ydotool") is None,
                         "ydotool present: 501 path not exercised")
    def test_input_endpoints_missing_ydotool(self):
        for path, payload in (
            ("/input/move", {"x": 10, "y": 10}),
            ("/input/click", {"button": "left"}),
            ("/input/type", {"text": "hi"}),
        ):
            status, body = self.request("POST", path, payload)
            self.assertEqual(status, 501, f"should 501: {path}")
            self.assertIn("ydotool", body["error"], f"should name ydotool: {path}")

    def test_input_validation(self):
        status, _ = self.request("POST", "/input/move", {"x": -1, "y": 5})
        self.assertEqual(status, 400)
        status, _ = self.request(
            "POST", "/input/click", {"button": "sideways"})
        self.assertEqual(status, 400)
        status, _ = self.request("POST", "/input/type", {"text": ""})
        self.assertEqual(status, 400)
        status, _ = self.request(
            "POST", "/input/click", {"x": 5})  # x without y
        self.assertEqual(status, 400)


class SandboxUnitTests(unittest.TestCase):
    def test_build_argv_structure(self):
        with mock.patch("hydra.sandbox.bwrap_path",
                        return_value="/usr/bin/bwrap"):
            argv = build_bwrap_argv(
                "make test", "/home/baji/hydra-workspace",
                "/home/baji/hydra-workspace/sub", no_network=True)
        self.assertEqual(argv[0], "/usr/bin/bwrap")
        self.assertIn("--unshare-net", argv)
        self.assertIn("--ro-bind", argv)
        self.assertIn("--bind", argv)
        ws_idx = argv.index("--bind")
        self.assertEqual(argv[ws_idx + 1], "/home/baji/hydra-workspace")
        self.assertEqual(argv[-3:], ["/bin/sh", "-c", "make test"])
        chdir_idx = argv.index("--chdir")
        self.assertEqual(argv[chdir_idx + 1],
                         "/home/baji/hydra-workspace/sub")

    def test_build_argv_network_opt_out(self):
        with mock.patch("hydra.sandbox.bwrap_path",
                        return_value="/usr/bin/bwrap"):
            argv = build_bwrap_argv("echo hi", "/ws", "/ws", no_network=False)
        self.assertNotIn("--unshare-net", argv)

    def test_build_argv_missing_bwrap(self):
        with mock.patch("hydra.sandbox.bwrap_path", return_value=None):
            with self.assertRaises(SandboxUnavailableError) as ctx:
                build_bwrap_argv("echo hi", "/ws", "/ws")
        self.assertIn("bubblewrap", str(ctx.exception))


class SearchUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "a.py").write_text("alpha\nbeta TODO\ngamma\n")
        (self.root / "b.txt").write_text("TODO in text\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_grep_fallback_backend(self):
        result = run_search("TODO", self.root, backend="grep")
        self.assertEqual(result["backend"], "grep")
        self.assertEqual(result["match_count"], 2)
        self.assertIsNone(result["matches"][0]["column"])

    def test_ripgrep_backend_when_present(self):
        if shutil.which("rg") is None:
            self.skipTest("rg not installed")
        result = run_search("TODO", self.root, backend="ripgrep")
        self.assertEqual(result["backend"], "ripgrep")
        self.assertEqual(result["match_count"], 2)
        self.assertIsNotNone(result["matches"][0]["column"])

    def test_search_invalid_regex(self):
        with self.assertRaises(SearchError):
            run_search("[invalid", self.root, backend="grep")

    def test_search_max_results_truncation(self):
        (self.root / "many.txt").write_text("hit\n" * 50)
        result = run_search("hit", self.root, backend="grep", max_results=5)
        self.assertEqual(result["match_count"], 5)
        self.assertTrue(result["truncated"])

    def test_search_file_root(self):
        result = run_search("TODO", self.root / "a.py", backend="grep")
        self.assertEqual(result["match_count"], 1)
        self.assertEqual(result["matches"][0]["line"], 2)


class GuiUnitTests(unittest.TestCase):
    def test_screenshot_no_session_env(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(GuiUnavailableError) as ctx:
                take_screenshot()
        self.assertIn("WAYLAND_DISPLAY", str(ctx.exception))

    def test_input_validation_errors(self):
        with self.assertRaises(GuiInputError):
            input_move(-1, 0)
        with self.assertRaises(GuiInputError):
            input_move("x", 0)
        with self.assertRaises(GuiInputError):
            input_click(None, None, button="sideways")
        with self.assertRaises(GuiInputError):
            input_click(None, None, clicks=9)
        with self.assertRaises(GuiInputError):
            input_type("")
        with self.assertRaises(GuiInputError):
            input_type("x" * 4001)

    @unittest.skipUnless(shutil.which("ydotool") is None,
                         "ydotool present: missing-tool path not exercised")
    def test_input_missing_ydotool(self):
        with self.assertRaises(GuiUnavailableError) as ctx:
            input_move(10, 10)
        self.assertIn("ydotool", str(ctx.exception))


class MemoryStoreV02Tests(unittest.TestCase):
    def test_delete_and_list_recent(self):
        with tempfile.TemporaryDirectory() as td:
            store = MemoryStore(Path(td) / "memory.db")
            try:
                m1 = store.add_memory("alpha fact", kind="fact")
                store.add_memory("beta note", kind="note")
                listed = store.list_recent()
                self.assertEqual(listed["total"], 2)
                self.assertEqual(listed["memories"][0]["kind"], "note")
                self.assertTrue(store.delete_memory(m1))
                self.assertFalse(store.delete_memory(m1))
                self.assertEqual(store.search("alpha")["results"], [])
                self.assertEqual(store.list_recent(kind="fact")["total"], 0)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
