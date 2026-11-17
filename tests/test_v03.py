"""HYDRA v0.3 tests (stdlib unittest only).

Run from the repo root:
    python3 -m unittest discover -s tests -v

Covers every v0.3 addition: the hand-rolled WebSocket client
(fragmentation, close, masking, large frames), the CDP browser
module against an independent fake CDP server (tests/fake_cdp.py),
persistent + detached jobs (including survival across a manager
restart and across a real daemon restart), /health resilience
fields, and the read-only /system/info + /git/status endpoints.

Live Chrome verification is out of scope here (no Chrome on the
test VM): the fake CDP server proves the protocol handling, and
docs/BROWSER_SETUP.md carries the laptop checklist.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import shutil
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_cdp import (  # noqa: E402
    FakeCDPServer,
    encode_frame_raw,
    make_png,
    read_frame_from_socket,
)

from hydra import __version__  # noqa: E402
from hydra.browser import (  # noqa: E402
    BrowserUnavailableError,
    CdpError,
    browser_click,
    browser_content,
    browser_evaluate,
    browser_navigate,
    browser_shot,
    browser_status,
    browser_type,
    validate_loopback,
)
from hydra.daemon import VERSION, make_server  # noqa: E402
from hydra.jobs import JobManager  # noqa: E402
from hydra.sysinfo import git_status, system_info  # noqa: E402
from hydra.websocket import (  # noqa: E402
    WebSocketClient,
    WebSocketClosed,
    WebSocketError,
    encode_frame,
)

TEST_TOKEN = "test-token-" + "x" * 24  # >= 20 chars, test-only


def _write_token_file(path: Path, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, (TEST_TOKEN + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, mode)


def _closed_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _wait_until(predicate, timeout: float = 8.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


# --------------------------------------------------------------------------
# WebSocket client
# --------------------------------------------------------------------------

class WebSocketUnitTests(unittest.TestCase):
    def _pair(self):
        client_sock, raw_sock = socket.socketpair()
        client_sock.settimeout(3)
        raw_sock.settimeout(3)
        client = WebSocketClient("127.0.0.1", 1, timeout=2.0)
        client._sock = client_sock
        return client, raw_sock

    def tearDown(self):
        pass  # sockets are closed by each test via client.close()

    def test_client_masks_frames(self):
        client, raw = self._pair()
        try:
            client.send_text("hello")
            head = raw.recv(2)
            self.assertTrue(head[1] & 0x80, "client frames must be masked")
            self.assertEqual(head[1] & 0x7F, 5)
            mask_key = raw.recv(4)
            masked = raw.recv(5)
            plain = bytes(b ^ mask_key[i % 4]
                          for i, b in enumerate(masked))
            self.assertEqual(plain, b"hello")
        finally:
            client.close()
            raw.close()

    def test_recv_fragmented_message(self):
        client, raw = self._pair()
        try:
            raw.sendall(encode_frame_raw(0x1, b"hello ", fin=False))
            raw.sendall(encode_frame_raw(0x0, b"world", fin=True))
            self.assertEqual(client.recv_message(), "hello world")
        finally:
            client.close()
            raw.close()

    def test_recv_ping_answered_with_pong(self):
        client, raw = self._pair()
        try:
            raw.sendall(encode_frame_raw(0x9, b"ping-data"))
            raw.sendall(encode_frame_raw(0x1, b"after-ping"))
            self.assertEqual(client.recv_message(), "after-ping")
            fin, opcode, payload = read_frame_from_socket(raw)
            self.assertEqual(opcode, 0xA)
            self.assertEqual(payload, b"ping-data")
        finally:
            client.close()
            raw.close()

    def test_recv_close_raises_and_replies(self):
        client, raw = self._pair()
        try:
            raw.sendall(encode_frame_raw(0x8, struct.pack("!H", 1000)))
            with self.assertRaises(WebSocketClosed):
                client.recv_message()
            fin, opcode, _payload = read_frame_from_socket(raw)
            self.assertEqual(opcode, 0x8)
        finally:
            client.close()
            raw.close()

    def test_recv_large_frames_16bit_and_64bit_lengths(self):
        client, raw = self._pair()
        try:
            big16 = "a" * 200
            raw.sendall(encode_frame_raw(0x1, big16.encode()))
            self.assertEqual(client.recv_message(), big16)
            big64 = "b" * 70000
            raw.sendall(encode_frame_raw(0x1, big64.encode()))
            self.assertEqual(client.recv_message(), big64)
        finally:
            client.close()
            raw.close()

    def test_recv_binary_message_returns_bytes(self):
        client, raw = self._pair()
        try:
            raw.sendall(encode_frame_raw(0x2, b"\x00\x01\x02"))
            self.assertEqual(client.recv_message(), b"\x00\x01\x02")
        finally:
            client.close()
            raw.close()

    def test_send_after_close_raises(self):
        client, raw = self._pair()
        client.close()
        raw.close()
        with self.assertRaises(WebSocketClosed):
            client.send_text("nope")

    def test_encode_frame_length_boundaries(self):
        # 125 bytes: 7-bit length.
        frame = encode_frame(0x1, b"x" * 125, mask=False)
        self.assertEqual(frame[1] & 0x7F, 125)
        self.assertEqual(len(frame), 2 + 125)
        # 126 bytes: 16-bit extended length.
        frame = encode_frame(0x1, b"x" * 126, mask=False)
        self.assertEqual(frame[1] & 0x7F, 126)
        self.assertEqual(struct.unpack("!H", frame[2:4])[0], 126)
        # 65536 bytes: 64-bit extended length.
        frame = encode_frame(0x1, b"x" * 65536, mask=False)
        self.assertEqual(frame[1] & 0x7F, 127)
        self.assertEqual(struct.unpack("!Q", frame[2:10])[0], 65536)

    def test_handshake_rejected(self):
        server_sock = socket.socket()
        server_sock.bind(("127.0.0.1", 0))
        server_sock.listen(1)
        port = server_sock.getsockname()[1]

        def serve_once():
            conn, _ = server_sock.accept()
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 404 Not Found\r\n"
                         b"Content-Length: 0\r\n\r\n")
            conn.close()
            server_sock.close()

        threading.Thread(target=serve_once, daemon=True).start()
        client = WebSocketClient("127.0.0.1", port, timeout=3.0)
        with self.assertRaises(WebSocketError):
            client.connect()

    def test_handshake_bad_accept_rejected(self):
        server_sock = socket.socket()
        server_sock.bind(("127.0.0.1", 0))
        server_sock.listen(1)
        port = server_sock.getsockname()[1]

        def serve_once():
            conn, _ = server_sock.accept()
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\n"
                         b"Upgrade: websocket\r\n"
                         b"Connection: Upgrade\r\n"
                         b"Sec-WebSocket-Accept: bogus\r\n\r\n")
            conn.close()
            server_sock.close()

        threading.Thread(target=serve_once, daemon=True).start()
        client = WebSocketClient("127.0.0.1", port, timeout=3.0)
        with self.assertRaises(WebSocketError):
            client.connect()

    def test_handshake_and_command_against_fake_cdp(self):
        server = FakeCDPServer().start()
        try:
            client = WebSocketClient(
                "127.0.0.1", server.port, "/devtools/page/page-1",
                timeout=5.0)
            with client:
                client.send_text(json.dumps(
                    {"id": 7, "method": "Page.navigate",
                     "params": {"url": "https://example.com/"}}))
                reply = json.loads(client.recv_message())
            self.assertEqual(reply["id"], 7)
            self.assertEqual(reply["result"]["frameId"], "frame-1")
        finally:
            server.stop()


# --------------------------------------------------------------------------
# CDP browser module (against the fake server)
# --------------------------------------------------------------------------

class BrowserModuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = FakeCDPServer().start()
        cls.port = cls.server.port

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def test_status_available(self):
        status = browser_status("127.0.0.1", self.port)
        self.assertTrue(status["available"])
        self.assertEqual(status["browser"], "FakeChrome/1.0")
        self.assertEqual(status["target_count"], 1)
        self.assertEqual(status["targets"][0]["id"], "page-1")

    def test_status_unavailable_is_a_value_not_an_exception(self):
        status = browser_status("127.0.0.1", _closed_port())
        self.assertFalse(status["available"])
        self.assertIn("chrome-debug.sh", status["error"])

    def test_navigate(self):
        before = len(self.server.commands)
        result = browser_navigate("127.0.0.1", self.port,
                                 "https://example.com/")
        self.assertEqual(result["frame_id"], "frame-1")
        command = self.server.commands[before]
        self.assertEqual(command["method"], "Page.navigate")
        self.assertEqual(command["params"]["url"], "https://example.com/")

    def test_navigate_about_blank_allowed(self):
        result = browser_navigate("127.0.0.1", self.port, "about:blank")
        self.assertEqual(result["url"], "about:blank")

    def test_navigate_file_url_refused(self):
        with self.assertRaises(ValueError):
            browser_navigate("127.0.0.1", self.port, "file:///etc/passwd")

    def test_navigate_other_schemes_refused(self):
        for url in ("chrome://settings", "data:text/html,<b>x</b>",
                    "javascript:alert(1)", "ftp://example.com/x"):
            with self.assertRaises(ValueError, msg=url):
                browser_navigate("127.0.0.1", self.port, url)

    def test_evaluate_value(self):
        result = browser_evaluate("127.0.0.1", self.port, "1+1")
        self.assertEqual(result["value"], "2")
        self.assertFalse(result["truncated"])

    def test_evaluate_exception_reported(self):
        result = browser_evaluate("127.0.0.1", self.port,
                                  "throw new Error('x')")
        self.assertIsNone(result["value"])
        self.assertIn("boom", result["exception"])

    def test_content_extract(self):
        result = browser_content("127.0.0.1", self.port)
        self.assertEqual(result["title"], "Fake Page")
        self.assertEqual(result["text"], "hello fake world")
        self.assertIn("fake", result["html"])
        self.assertEqual(result["url"], "https://example.com/")
        self.assertFalse(result["text_truncated"])

    def test_shot_returns_real_png(self):
        result = browser_shot("127.0.0.1", self.port)
        self.assertTrue(result["png"].startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual((result["width"], result["height"]), (2, 2))
        self.assertEqual(result["bytes"], len(result["png"]))

    def test_click_sends_press_and_release(self):
        before = len(self.server.commands)
        result = browser_click("127.0.0.1", self.port, 100, 200)
        self.assertEqual((result["x"], result["y"]), (100, 200))
        sent = self.server.commands[before:]
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0]["params"]["type"], "mousePressed")
        self.assertEqual(sent[1]["params"]["type"], "mouseReleased")

    def test_click_rejects_bad_coordinates(self):
        for x, y in ((-1, 0), (0, -1), (70000, 0), ("100", 200)):
            with self.assertRaises(ValueError, msg=f"{x},{y}"):
                browser_click("127.0.0.1", self.port, x, y)

    def test_type_ok_and_too_long_refused(self):
        result = browser_type("127.0.0.1", self.port, "hello")
        self.assertEqual(result["text_len"], 5)
        with self.assertRaises(ValueError):
            browser_type("127.0.0.1", self.port, "x" * 4001)

    def test_unknown_target_id(self):
        with self.assertRaises(CdpError):
            browser_navigate("127.0.0.1", self.port,
                            "https://example.com/", "no-such-target")

    def test_no_page_targets(self):
        empty = FakeCDPServer(page_targets=[]).start()
        try:
            with self.assertRaises(CdpError):
                browser_navigate("127.0.0.1", empty.port,
                                "https://example.com/")
        finally:
            empty.stop()

    def test_events_skipped_while_waiting_for_reply(self):
        chatty = FakeCDPServer(send_event_before_reply=True).start()
        try:
            result = browser_evaluate("127.0.0.1", chatty.port, "1+1")
            self.assertEqual(result["value"], "2")
        finally:
            chatty.stop()

    def test_unavailable_browser_raises_for_actions(self):
        with self.assertRaises(BrowserUnavailableError):
            browser_navigate("127.0.0.1", _closed_port(),
                            "https://example.com/")

    def test_validate_loopback(self):
        self.assertEqual(validate_loopback("127.0.0.1"), "127.0.0.1")
        self.assertEqual(validate_loopback("localhost"), "localhost")
        self.assertEqual(validate_loopback("::1"), "::1")
        for host in ("8.8.8.8", "100.74.209.52", "192.168.1.1",
                     "example.com", "0.0.0.0"):
            with self.assertRaises(ValueError, msg=host):
                validate_loopback(host)

    def test_non_loopback_refused_before_any_socket(self):
        with self.assertRaises(ValueError):
            browser_status("8.8.8.8", 9222)


# --------------------------------------------------------------------------
# Persistent + detached jobs (JobManager directly)
# --------------------------------------------------------------------------

class JobsPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name) / "data"
        self.data_dir.mkdir()
        self.ws = Path(self.tmp.name) / "ws"
        self.ws.mkdir()
        self.manager = JobManager(data_dir=self.data_dir)

    def tearDown(self):
        # Kill everything, including detached jobs (kill_all skips them).
        for summary in self.manager.list_jobs():
            if summary["status"] == "running":
                self.manager.kill(summary["job_id"])
        self.manager.close()
        self.tmp.cleanup()

    def _start(self, command, **kwargs):
        return self.manager.start(
            ["/bin/sh", "-c", command], command, self.ws,
            {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            timeout_seconds=kwargs.pop("timeout_seconds", 60),
            **kwargs)

    def _wait_done(self, job_id, timeout=8.0):
        def done():
            job = self.manager.get(job_id)
            return job if job is not None and job.status != "running" else None
        found = _wait_until(done, timeout=timeout)
        self.assertIsNotNone(found, f"job {job_id} did not finish")
        return found

    def test_job_record_persisted_in_sqlite(self):
        job = self._start("echo hello-persist")
        self._wait_done(job.job_id)
        db = sqlite3.connect(str(self.data_dir / "jobs.db"))
        row = db.execute(
            "SELECT status, returncode FROM jobs WHERE job_id=?",
            (job.job_id,)).fetchone()
        db.close()
        self.assertEqual(row[0], "done")
        self.assertEqual(row[1], 0)

    def test_log_files_hold_output(self):
        job = self._start("echo out-line; echo err-line >&2")
        self._wait_done(job.job_id)
        out = (self.data_dir / "job-logs" / f"{job.job_id}.out").read_text()
        err = (self.data_dir / "job-logs" / f"{job.job_id}.err").read_text()
        self.assertIn("out-line", out)
        self.assertIn("err-line", err)
        snap = job.snapshot()
        self.assertIn("out-line", snap["stdout"])
        self.assertIn("err-line", snap["stderr"])

    def test_output_cap_and_truncated_flag(self):
        job = self._start(
            "python3 -c \"import sys; sys.stdout.write('x'*300000)\"")
        self._wait_done(job.job_id)
        snap = job.snapshot()
        self.assertTrue(snap["truncated"])
        self.assertEqual(len(snap["stdout"]), 200 * 1024)

    def test_job_survives_manager_restart(self):
        job = self._start("sleep 30")
        job_id, pid = job.job_id, job.pid
        self.manager.close()  # simulate daemon restart (no kill_all)
        manager2 = JobManager(data_dir=self.data_dir)
        try:
            recovered = manager2.get(job_id)
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered.status, "running")
            self.assertTrue(recovered.recovered)
            self.assertEqual(recovered.pid, pid)
            os.kill(pid, 0)  # process really alive
            killed, _ = manager2.kill(job_id)
            self.assertTrue(killed)
            gone = _wait_until(
                lambda: _pid_gone(pid), timeout=5.0)
            self.assertTrue(gone, "process did not die after kill")
        finally:
            manager2.close()
        self.manager = JobManager(data_dir=self.data_dir)

    def test_detached_job_survives_kill_all(self):
        job = self._start("sleep 30", detached=True)
        self.assertEqual(self.manager.kill_all(), 0)
        self.assertEqual(self.manager.get(job.job_id).status, "running")
        killed, _ = self.manager.kill(job.job_id)
        self.assertTrue(killed)

    def test_non_detached_job_killed_by_kill_all(self):
        job = self._start("sleep 30")
        self.assertEqual(self.manager.kill_all(), 1)
        self.assertEqual(self.manager.get(job.job_id).status, "killed")

    def test_dead_job_finalised_on_reload_with_unknown_exit(self):
        # Simulate a crash: a "running" row whose process is already gone.
        # Reap a pid ourselves so it is certainly dead:
        sleeper = subprocess.Popen(["true"])
        dead_pid = sleeper.pid
        sleeper.wait()
        db = sqlite3.connect(str(self.data_dir / "jobs.db"))
        db.execute(
            """INSERT INTO jobs (job_id, command, cwd, pid, pgid,
                   proc_starttime, status, returncode, timed_out,
                   sandboxed, detached, started_at, finished_at,
                   deadline_epoch)
               VALUES (?, ?, ?, ?, ?, ?, 'running', NULL, 0, 0, 0, ?, NULL, NULL)""",
            ("deadbeefcafe0001", "sleep 60", str(self.ws),
             dead_pid, dead_pid, "bogus-starttime", time.time()))
        db.commit()
        db.close()
        self.manager.close()
        manager2 = JobManager(data_dir=self.data_dir)
        try:
            job = manager2.get("deadbeefcafe0001")
            self.assertIsNotNone(job)
            self.assertEqual(job.status, "done")
            self.assertIsNone(job.returncode)  # unknown, not faked
            self.assertTrue(job.recovered)
        finally:
            manager2.close()
        self.manager = JobManager(data_dir=self.data_dir)

    def test_deadline_enforced_after_restart(self):
        job = self._start("sleep 60", timeout_seconds=2)
        job_id = job.job_id
        self.manager.close()
        manager2 = JobManager(data_dir=self.data_dir)
        try:
            def killed():
                found = manager2.get(job_id)
                return (found if found is not None
                        and found.status == "killed"
                        and found.timed_out else None)
            found = _wait_until(killed, timeout=8.0)
            self.assertIsNotNone(found, "deadline was not enforced")
        finally:
            manager2.close()
        self.manager = JobManager(data_dir=self.data_dir)

    def test_prune_deletes_db_rows_and_log_files(self):
        small = JobManager(max_jobs=2, data_dir=self.data_dir)
        try:
            ids = []
            for i in range(3):
                job = small.start(
                    ["/bin/sh", "-c", f"echo job-{i}"], f"echo job-{i}",
                    self.ws, {"PATH": "/usr/bin:/bin"},
                    timeout_seconds=60)
                ids.append(job.job_id)
                _wait_until(
                    lambda j=job: small.get(j.job_id).status != "running",
                    timeout=5.0)
            # Starting a 4th job prunes the oldest finished one.
            job4 = small.start(
                ["/bin/sh", "-c", "echo job-3"], "echo job-3",
                self.ws, {"PATH": "/usr/bin:/bin"}, timeout_seconds=60)
            _wait_until(
                lambda: small.get(job4.job_id).status != "running",
                timeout=5.0)
            self.assertIsNone(small.get(ids[0]))
            db = sqlite3.connect(str(self.data_dir / "jobs.db"))
            count = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            db.close()
            # Pruning runs before each start, so the table holds at
            # most max_jobs + 1 rows; the oldest one is gone.
            self.assertEqual(count, 3)
            self.assertFalse(
                (self.data_dir / "job-logs" / f"{ids[0]}.out").exists())
        finally:
            small.close()

    def test_running_job_cap(self):
        small = JobManager(max_jobs=2, data_dir=self.data_dir)
        try:
            j1 = small.start(["/bin/sh", "-c", "sleep 30"], "sleep 30",
                             self.ws, {"PATH": "/usr/bin:/bin"},
                             timeout_seconds=60)
            j2 = small.start(["/bin/sh", "-c", "sleep 30"], "sleep 30",
                             self.ws, {"PATH": "/usr/bin:/bin"},
                             timeout_seconds=60)
            with self.assertRaises(RuntimeError):
                small.start(["/bin/sh", "-c", "sleep 30"], "sleep 30",
                            self.ws, {"PATH": "/usr/bin:/bin"},
                            timeout_seconds=60)
            small.kill(j1.job_id)
            small.kill(j2.job_id)
        finally:
            small.close()

    def test_counts(self):
        self._start("sleep 30")
        job2 = self._start("echo quick")
        self._wait_done(job2.job_id)
        counts = self.manager.counts()
        self.assertEqual(counts["running"], 1)
        self.assertEqual(counts["finished"], 1)


def _pid_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


# --------------------------------------------------------------------------
# Read-only system/git info (direct)
# --------------------------------------------------------------------------

class SysinfoUnitTests(unittest.TestCase):
    def test_system_info_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            info = system_info(Path(tmp))
        self.assertGreater(info["disk"]["free_bytes"], 0)
        self.assertEqual(info["disk"]["path"], tmp)
        self.assertEqual(set(info["tools"]),
                         {"rg", "bwrap", "ffmpeg", "ydotool",
                          "spectacle", "grim"})
        self.assertTrue(all(isinstance(v, bool)
                            for v in info["tools"].values()))
        self.assertTrue(info["load_average"] is None
                        or len(info["load_average"]) == 3)
        self.assertTrue(info["memory"] is None
                        or info["memory"]["total_bytes"] > 0)

    def test_git_status_rejects_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                git_status(Path(tmp), "../..")
            with self.assertRaises(FileNotFoundError):
                git_status(Path(tmp), "no-such-dir")


# --------------------------------------------------------------------------
# HTTP daemon end-to-end
# --------------------------------------------------------------------------

class HttpDaemonV03Tests(unittest.TestCase):
    """End-to-end tests against a real v0.3 daemon instance."""

    @classmethod
    def setUpClass(cls):
        cls.fake_cdp = FakeCDPServer().start()
        cls.cdp_port = cls.fake_cdp.port

    @classmethod
    def tearDownClass(cls):
        cls.fake_cdp.stop()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws = root / "ws"
        self.ws.mkdir()
        (self.ws / "hello.txt").write_text("hello hydra\nsecond line\n")
        data_dir = root / "data"
        data_dir.mkdir()
        self.data_dir = data_dir
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
        self._start_server()

    def _start_server(self):
        self.server = make_server(self.config, TEST_TOKEN)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _stop_server(self, kill_jobs: bool = True):
        handler_cls = self.server.RequestHandlerClass
        try:
            if kill_jobs:
                handler_cls.jobs.kill_all()
            handler_cls.jobs.close()
        except Exception:
            pass
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        try:
            handler_cls.store.close()
        except Exception:
            pass

    def tearDown(self):
        # Kill detached leftovers too (kill_all skips them).
        try:
            jobs = self.server.RequestHandlerClass.jobs
            for summary in jobs.list_jobs():
                if summary["status"] == "running":
                    jobs.kill(summary["job_id"])
        except Exception:
            pass
        self._stop_server()
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

    # -- version + health --------------------------------------------------

    def test_version_is_0_4_0(self):
        self.assertEqual(VERSION, "0.4.0")
        self.assertEqual(__version__, "0.4.0")

    def test_health_reports_uptime_and_job_counts(self):
        status, body = self.request("GET", "/health", token="none")
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], "0.4.0")
        self.assertIsInstance(body["uptime_seconds"], (int, float))
        self.assertGreaterEqual(body["uptime_seconds"], 0)
        self.assertEqual(body["jobs_running"], 0)
        self.assertEqual(body["jobs_finished"], 0)
        _, started = self.request("POST", "/jobs/start",
                                  {"command": "echo hi"})
        self.poll_until_done(started["job_id"])
        _, body = self.request("GET", "/health", token="none")
        self.assertEqual(body["jobs_finished"], 1)

    # -- /system/info + /git/status ----------------------------------------

    def test_system_info(self):
        status, body = self.request("POST", "/system/info", {})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertGreater(body["disk"]["free_bytes"], 0)
        self.assertEqual(set(body["tools"]),
                         {"rg", "bwrap", "ffmpeg", "ydotool",
                          "spectacle", "grim"})

    def test_system_info_requires_auth(self):
        status, _ = self.request("POST", "/system/info", {}, token="none")
        self.assertEqual(status, 401)

    def test_git_status_non_repo(self):
        status, body = self.request("POST", "/git/status", {"path": "."})
        self.assertEqual(status, 200)
        self.assertFalse(body["is_repo"])
        self.assertIsNone(body["branch"])
        self.assertEqual(body["changes"], [])

    def test_git_status_requires_auth(self):
        status, _ = self.request("POST", "/git/status", {}, token="none")
        self.assertEqual(status, 401)

    def test_git_status_outside_workspace_refused(self):
        status, _ = self.request("POST", "/git/status", {"path": ".."})
        self.assertEqual(status, 403)
        status, _ = self.request("POST", "/git/status", {"path": "/etc"})
        self.assertEqual(status, 403)

    def test_git_status_missing_path_404(self):
        status, _ = self.request(
            "POST", "/git/status", {"path": "no-such-dir"})
        self.assertEqual(status, 404)

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_git_status_repo(self):
        def git(*args):
            subprocess.run(["git", *args], cwd=self.ws, check=True,
                           capture_output=True, timeout=15)
        git("init", "-q")
        git("config", "user.email", "test@example.com")
        git("config", "user.name", "Test")
        (self.ws / "tracked.txt").write_text("v1\n")
        git("add", "-A")
        git("commit", "-q", "-m", "init")
        status, body = self.request("POST", "/git/status", {"path": "."})
        self.assertEqual(status, 200)
        self.assertTrue(body["is_repo"])
        self.assertTrue(body["branch"])
        self.assertEqual(len(body["head"]), 40)
        self.assertIsNone(body["upstream"])
        self.assertEqual(body["change_count"], 0)
        (self.ws / "tracked.txt").write_text("v2\n")
        (self.ws / "untracked.txt").write_text("new\n")
        _, body = self.request("POST", "/git/status", {"path": "."})
        self.assertEqual(body["change_count"], 2)
        by_path = {c["path"]: c["status"] for c in body["changes"]}
        self.assertEqual(by_path["tracked.txt"], "M")
        self.assertEqual(by_path["untracked.txt"], "??")

    # -- persistent / detached jobs over HTTP ------------------------------

    def test_jobs_start_detached_field_and_snapshot(self):
        status, body = self.request(
            "POST", "/jobs/start", {"command": "echo hi", "detached": True})
        self.assertEqual(status, 200)
        self.assertTrue(body["detached"])
        done = self.poll_until_done(body["job_id"])
        self.assertTrue(done["detached"])
        self.assertIsNotNone(done["pid"])
        self.assertFalse(done["recovered"])
        self.assertIn("hi", done["stdout"])

    def test_detached_job_survives_daemon_restart(self):
        _, started = self.request(
            "POST", "/jobs/start",
            {"command": "sleep 30", "detached": True})
        job_id = started["job_id"]
        _, before = self.request("POST", "/jobs/poll", {"job_id": job_id})
        self.assertEqual(before["status"], "running")
        pid = before["pid"]
        self.assertIsNotNone(pid)

        # Restart the daemon WITHOUT killing jobs (like a crash + restart).
        self._stop_server(kill_jobs=False)
        self._start_server()

        _, after = self.request("POST", "/jobs/poll", {"job_id": job_id})
        self.assertEqual(after["status"], "running")
        self.assertTrue(after["recovered"])
        self.assertEqual(after["pid"], pid)
        os.kill(pid, 0)  # process really survived
        _, killed = self.request("POST", "/jobs/kill", {"job_id": job_id})
        self.assertTrue(killed["killed"])
        gone = _wait_until(lambda: _pid_gone(pid), timeout=5.0)
        self.assertTrue(gone)

    def test_finished_job_still_pollable_after_restart(self):
        _, started = self.request(
            "POST", "/jobs/start", {"command": "echo after-restart"})
        job_id = started["job_id"]
        self.poll_until_done(job_id)
        self._stop_server(kill_jobs=False)
        self._start_server()
        status, body = self.request("POST", "/jobs/poll", {"job_id": job_id})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "done")
        self.assertIn("after-restart", body["stdout"])

    def test_shutdown_kills_non_detached_only(self):
        _, plain = self.request(
            "POST", "/jobs/start", {"command": "sleep 30"})
        _, detached = self.request(
            "POST", "/jobs/start",
            {"command": "sleep 30", "detached": True})
        _, poll = self.request(
            "POST", "/jobs/poll", {"job_id": detached["job_id"]})
        detached_pid = poll["pid"]
        status, body = self.request(
            "POST", "/shutdown", {"confirm": True})
        self.assertEqual(status, 200)
        self.assertEqual(body["jobs_killed"], 1)
        os.kill(detached_pid, 0)  # detached job survived shutdown
        # Clean up the survivor directly (the daemon is gone).
        try:
            os.killpg(detached_pid, signal.SIGKILL)
        except OSError:
            pass
        _wait_until(lambda: _pid_gone(detached_pid), timeout=5.0)
        # The serve thread exits on its own; tearDown handles the rest.

    # -- browser endpoints over HTTP ----------------------------------------

    def test_browser_status_probe(self):
        status, body = self.request(
            "POST", "/browser/status", {"port": self.cdp_port})
        self.assertEqual(status, 200)
        self.assertTrue(body["available"])
        self.assertEqual(body["target_count"], 1)
        status, body = self.request(
            "POST", "/browser/status", {"port": _closed_port()})
        self.assertEqual(status, 200)  # a probe: absent is a value
        self.assertFalse(body["available"])

    def test_browser_endpoints_end_to_end(self):
        port = self.cdp_port
        status, body = self.request(
            "POST", "/browser/navigate",
            {"port": port, "url": "https://example.com/"})
        self.assertEqual(status, 200)
        self.assertEqual(body["frame_id"], "frame-1")
        status, body = self.request(
            "POST", "/browser/evaluate",
            {"port": port, "expression": "document.title"})
        self.assertEqual(status, 200)
        self.assertEqual(body["value"], '"Fake Page"')
        status, body = self.request(
            "POST", "/browser/content", {"port": port})
        self.assertEqual(status, 200)
        self.assertEqual(body["text"], "hello fake world")
        status, body = self.request(
            "POST", "/browser/click", {"port": port, "x": 10, "y": 20})
        self.assertEqual(status, 200)
        self.assertEqual((body["x"], body["y"]), (10, 20))
        status, body = self.request(
            "POST", "/browser/type", {"port": port, "text": "hi"})
        self.assertEqual(status, 200)
        self.assertEqual(body["text_len"], 2)

    def test_browser_shot_save_to_and_base64(self):
        status, body = self.request(
            "POST", "/browser/shot",
            {"port": self.cdp_port, "save_to": "shots/page.png"})
        self.assertEqual(status, 200)
        self.assertEqual(body["backend"], "cdp")
        self.assertEqual((body["width"], body["height"]), (2, 2))
        saved = self.ws / "shots" / "page.png"
        self.assertTrue(saved.exists())
        self.assertTrue(saved.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
        status, body = self.request(
            "POST", "/browser/shot", {"port": self.cdp_port})
        self.assertEqual(status, 200)
        png = base64.b64decode(body["data_base64"])
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_browser_shot_save_to_escape_refused(self):
        status, _ = self.request(
            "POST", "/browser/shot",
            {"port": self.cdp_port, "save_to": "../escape.png"})
        self.assertEqual(status, 403)

    def test_browser_requires_auth(self):
        status, _ = self.request(
            "POST", "/browser/status", {"port": self.cdp_port},
            token="none")
        self.assertEqual(status, 401)
        status, _ = self.request(
            "POST", "/browser/navigate",
            {"port": self.cdp_port, "url": "https://example.com/"},
            token="none")
        self.assertEqual(status, 401)

    def test_browser_non_loopback_refused(self):
        for host in ("8.8.8.8", "100.74.209.52", "example.com"):
            status, body = self.request(
                "POST", "/browser/status", {"host": host, "port": 9222})
            self.assertEqual(status, 403, msg=host)
            self.assertIn("loopback", body["error"])

    def test_browser_unavailable_is_501(self):
        status, body = self.request(
            "POST", "/browser/navigate",
            {"port": _closed_port(), "url": "https://example.com/"})
        self.assertEqual(status, 501)
        self.assertIn("chrome-debug.sh", body["error"])

    def test_browser_navigate_file_url_is_400(self):
        status, _ = self.request(
            "POST", "/browser/navigate",
            {"port": self.cdp_port, "url": "file:///etc/passwd"})
        self.assertEqual(status, 400)

    def test_browser_audit_never_contains_typed_text_or_content(self):
        secret = "sekrit-typed-text-12345"
        self.request("POST", "/browser/type",
                     {"port": self.cdp_port, "text": secret})
        self.request("POST", "/browser/content", {"port": self.cdp_port})
        audit = (self.data_dir / "audit.jsonl").read_text()
        self.assertNotIn(secret, audit)
        self.assertNotIn("hello fake world", audit)
        self.assertIn("/browser/type", audit)

    def test_png_builder_valid(self):
        png = make_png(3, 2)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        width, height = struct.unpack("!II", png[16:24])
        self.assertEqual((width, height), (3, 2))


if __name__ == "__main__":
    unittest.main()
