"""HYDRA v0.4 Tailscale-auto tests (stdlib unittest only).

Run from the repo root:
    python3 -m unittest discover -s tests -v

Covers the Tailscale-auto additions folded into v0.4: the
``--bind tailscale`` mode (fail-closed resolution via a fake
``tailscale`` binary on PATH), the read-only ``/system/tailscale``
and ``/system/doctor`` endpoints, the ``tailscale`` fields on
``GET /health``, and the laptop helper scripts
(``pair.sh``, ``setup-tailscale.sh``, ``kill-switch.sh``).

No real Tailscale, tailscaled, or systemd user session is needed:
every test that touches the ``tailscale`` CLI uses a fake binary
in a temporary directory placed first on PATH, and every test
that touches ``systemctl`` uses a fake one the same way. The
no-secret guarantee is asserted directly: the token value, node
keys, and peer identities must never appear in any endpoint
response, doctor report, script output, or audit entry.

Live verification on Baji's laptop (real tailscaled, real unit,
real --bind tailscale start) stays on the docs/TAILSCALE_AUTO.md
checklist and is NOT covered here.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hydra import __version__  # noqa: E402
from hydra.daemon import Config, VERSION, make_server, validate_bind  # noqa: E402
from hydra.doctor import run_doctor  # noqa: E402
from hydra.tailscale import (  # noqa: E402
    TailscaleError,
    TailscaleUnavailableError,
    parse_tailscale_status_json,
    resolve_tailscale_ip,
    tailscale_status,
    tailscale_summary,
)

TEST_TOKEN = "ts-auto-test-token-0123456789abcdef"
REPO_ROOT = Path(__file__).resolve().parent.parent

_FULL_STATUS_JSON = (
    '{"Version": "1.104.7", "BackendState": "Running", '
    '"Self": {"DNSName": "arch.tail123.ts.net", "HostName": "arch", '
    '"TailscaleIPs": ["100.74.209.52", "fd7a:115c:a1e0::1"], '
    '"Online": true, "KeyExpiry": "2026-06-01T00:00:00Z"}, '
    '"Peer": {'
    '"tskey-node-FAKEKEY-1111": {"HostName": "secret-peer-one"}, '
    '"tskey-node-FAKEKEY-2222": {"HostName": "secret-peer-two"}}}'
)
_NEEDSLOGIN_JSON = (
    '{"Version": "1.104.7", "BackendState": "NeedsLogin", '
    '"Self": {"HostName": "arch", "TailscaleIPs": []}, "Peer": {}}'
)


def _write_token_file(path: Path, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, (TEST_TOKEN + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, mode)


def _write_exe(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _write_fake_tailscale(bin_dir: Path, mode: str = "up") -> None:
    """A fake `tailscale` shell script. The mode is embedded in the
    script (not read from the environment) because HYDRA runs the
    CLI with a scrubbed environment that drops test variables."""
    script = f"""#!/bin/sh
MODE="{mode}"
if [ "$1" = "ip" ]; then
  case "$MODE" in
    down) echo "failed to connect to local tailscaled" >&2; exit 1;;
    badip) echo "8.8.8.8";;
    empty) :;;
    *) echo "100.74.209.52";;
  esac
  exit 0
fi
if [ "$1" = "status" ]; then
  case "$MODE" in
    down) echo "failed to connect to local tailscaled" >&2; exit 1;;
    badjson) printf '%s' 'not json{{';;
    needslogin) printf '%s' '{_NEEDSLOGIN_JSON}';;
    *) printf '%s' '{_FULL_STATUS_JSON}';;
  esac
  exit 0
fi
echo "unknown tailscale command" >&2
exit 1
"""
    _write_exe(bin_dir / "tailscale", script)


def _write_fake_systemctl(bin_dir: Path, enabled: str = "enabled",
                          active: str = "active") -> None:
    script = f"""#!/bin/sh
if [ -n "${{FAKE_SYSTEMCTL_LOG:-}}" ]; then echo "$*" >> "$FAKE_SYSTEMCTL_LOG"; fi
if [ "$1" = "--user" ] && [ "$2" = "is-enabled" ]; then echo "{enabled}"; exit 0; fi
if [ "$1" = "--user" ] && [ "$2" = "is-active" ]; then echo "{active}"; exit 0; fi
exit 0
"""
    _write_exe(bin_dir / "systemctl", script)


def _write_fake_tool(bin_dir: Path, name: str) -> None:
    _write_exe(bin_dir / name, "#!/bin/sh\nexit 0\n")


def _make_fake_bin(root: Path, ts_mode: str | None = "up",
                   with_systemctl: bool = True,
                   unit_enabled: str = "enabled",
                   unit_active: str = "active",
                   tools: tuple[str, ...] = ("melt", "ffmpeg", "ffprobe",
                                             "kdenlive", "bwrap", "rg"),
                   ) -> Path:
    """A directory of fake binaries. ts_mode=None means no tailscale
    binary at all (the 'missing' case)."""
    bin_dir = root / f"bin-{ts_mode or 'notailscale'}-{len(os.listdir(root))}"
    bin_dir.mkdir(parents=True, exist_ok=True)
    if ts_mode is not None:
        _write_fake_tailscale(bin_dir, ts_mode)
    if with_systemctl:
        _write_fake_systemctl(bin_dir, unit_enabled, unit_active)
    for tool in tools:
        _write_fake_tool(bin_dir, tool)
    return bin_dir


def _clean_hydra_env() -> dict:
    """Env patch that neutralises HYDRA_* config overrides so Config
    tests see only their explicit CLI arguments."""
    return {"HYDRA_CONFIG": "", "HYDRA_BIND": "", "HYDRA_PORT": "",
            "HYDRA_WORKSPACE": "", "HYDRA_TOKEN_FILE": "",
            "HYDRA_DATA_DIR": ""}


_DiskUsage = namedtuple("usage", ["total", "used", "free"])


def _roomy_disk(_path):
    """A disk with plenty free, so doctor tests are not at the mercy
    of the test machine's /tmp (a 512 MiB tmpfs on the Muse VM)."""
    return _DiskUsage(total=100 * (1 << 30), used=50 * (1 << 30),
                      free=50 * (1 << 30))


# --------------------------------------------------------------------------
# Bind resolution + validate_bind
# --------------------------------------------------------------------------

class BindResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_resolve_tailscale_ip_success(self):
        bin_dir = _make_fake_bin(self.root, "up")
        with mock.patch.dict(os.environ, {"PATH": str(bin_dir)}):
            self.assertEqual(resolve_tailscale_ip(), "100.74.209.52")

    def test_resolve_tailscale_ip_down_fails_closed(self):
        bin_dir = _make_fake_bin(self.root, "down")
        with mock.patch.dict(os.environ, {"PATH": str(bin_dir)}):
            with self.assertRaises(TailscaleError) as ctx:
                resolve_tailscale_ip()
            self.assertIn("tailscale up", str(ctx.exception))

    def test_resolve_tailscale_ip_rejects_non_cgnat(self):
        bin_dir = _make_fake_bin(self.root, "badip")
        with mock.patch.dict(os.environ, {"PATH": str(bin_dir)}):
            with self.assertRaises(TailscaleError):
                resolve_tailscale_ip()

    def test_resolve_tailscale_ip_empty_output_fails(self):
        bin_dir = _make_fake_bin(self.root, "empty")
        with mock.patch.dict(os.environ, {"PATH": str(bin_dir)}):
            with self.assertRaises(TailscaleError):
                resolve_tailscale_ip()

    def test_resolve_tailscale_ip_missing_binary(self):
        bin_dir = _make_fake_bin(self.root, None)
        with mock.patch.dict(os.environ, {"PATH": str(bin_dir)}):
            with self.assertRaises(TailscaleUnavailableError) as ctx:
                resolve_tailscale_ip()
            self.assertIn("pacman -S tailscale", str(ctx.exception))

    def test_validate_bind_still_refuses_unsafe(self):
        validate_bind("127.0.0.1", allow_public=False)
        validate_bind("100.100.1.2", allow_public=False)
        with self.assertRaises(ValueError):
            validate_bind("0.0.0.0", allow_public=False)
        with self.assertRaises(ValueError):
            validate_bind("192.168.1.5", allow_public=False)
        # The literal word "tailscale" is NOT a bind address: it must
        # be resolved by Config first, never passed to the socket.
        with self.assertRaises(ValueError):
            validate_bind("tailscale", allow_public=False)

    def _config_args(self, bind: str) -> SimpleNamespace:
        ws = self.root / "ws"
        data = self.root / "data"
        return SimpleNamespace(
            bind=bind, port=None, workspace=str(ws),
            token_file=str(data / "token"), data_dir=str(data),
            config="", allow_public=False, allow_systemctl_manage=False)

    def test_config_bind_tailscale_resolves(self):
        bin_dir = _make_fake_bin(self.root, "up")
        env = {"PATH": str(bin_dir), **_clean_hydra_env()}
        with mock.patch.dict(os.environ, env):
            config = Config(self._config_args("tailscale"))
        self.assertEqual(config.bind, "100.74.209.52")
        self.assertEqual(config.bind_mode, "tailscale")
        self.assertEqual(config.bind_raw, "tailscale")

    def test_config_bind_tailscale_down_refuses(self):
        bin_dir = _make_fake_bin(self.root, "down")
        env = {"PATH": str(bin_dir), **_clean_hydra_env()}
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(TailscaleError):
                Config(self._config_args("tailscale"))

    def test_config_default_bind_unchanged(self):
        env = {"PATH": os.environ.get("PATH", ""), **_clean_hydra_env()}
        with mock.patch.dict(os.environ, env):
            config = Config(self._config_args("127.0.0.1"))
        self.assertEqual(config.bind, "127.0.0.1")
        self.assertEqual(config.bind_mode, "explicit")


# --------------------------------------------------------------------------
# Status parsing + summary
# --------------------------------------------------------------------------

class StatusParsingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_full_status_drops_keys_and_peers(self):
        result = parse_tailscale_status_json(_FULL_STATUS_JSON)
        self.assertTrue(result["connected"])
        self.assertEqual(result["backend_state"], "Running")
        self.assertEqual(result["self_hostname"], "arch")
        self.assertEqual(result["self_ip"], "100.74.209.52")
        self.assertEqual(result["version"], "1.104.7")
        self.assertEqual(result["peer_count"], 2)
        blob = json.dumps(result)
        self.assertNotIn("tskey", blob)
        self.assertNotIn("FAKEKEY", blob)
        self.assertNotIn("secret-peer-one", blob)
        self.assertNotIn("secret-peer-two", blob)
        self.assertNotIn("tail123", blob)
        self.assertEqual(set(result),
                         {"connected", "backend_state", "self_hostname",
                          "self_ip", "version", "peer_count"})

    def test_parse_needs_login_is_not_an_error(self):
        result = parse_tailscale_status_json(_NEEDSLOGIN_JSON)
        self.assertFalse(result["connected"])
        self.assertEqual(result["backend_state"], "NeedsLogin")
        self.assertIsNone(result["self_ip"])
        self.assertEqual(result["peer_count"], 0)

    def test_parse_bad_json_and_non_object(self):
        with self.assertRaises(TailscaleError):
            parse_tailscale_status_json("not json{")
        with self.assertRaises(TailscaleError):
            parse_tailscale_status_json("[1, 2, 3]")

    def test_parse_ipv6_first_still_finds_ipv4(self):
        raw = json.dumps({"BackendState": "Running",
                          "Self": {"HostName": "arch",
                                   "TailscaleIPs": ["fd7a:115c:a1e0::1",
                                                    "100.74.209.52"]},
                          "Peer": {}})
        result = parse_tailscale_status_json(raw)
        self.assertTrue(result["connected"])
        self.assertEqual(result["self_ip"], "100.74.209.52")

    def test_tailscale_status_via_fake_binary(self):
        bin_dir = _make_fake_bin(self.root, "up")
        with mock.patch.dict(os.environ, {"PATH": str(bin_dir)}):
            result = tailscale_status()
        self.assertTrue(result["connected"])
        self.assertEqual(result["peer_count"], 2)

    def test_tailscale_status_down_and_missing(self):
        down_dir = _make_fake_bin(self.root, "down")
        with mock.patch.dict(os.environ, {"PATH": str(down_dir)}):
            with self.assertRaises(TailscaleError):
                tailscale_status()
        missing_dir = _make_fake_bin(self.root, None)
        with mock.patch.dict(os.environ, {"PATH": str(missing_dir)}):
            with self.assertRaises(TailscaleUnavailableError):
                tailscale_status()

    def test_tailscale_summary_never_raises(self):
        up_dir = _make_fake_bin(self.root, "up")
        with mock.patch.dict(os.environ, {"PATH": str(up_dir)}):
            summary = tailscale_summary()
        self.assertEqual(summary, {"present": True, "connected": True,
                                   "ip": "100.74.209.52"})
        down_dir = _make_fake_bin(self.root, "down")
        with mock.patch.dict(os.environ, {"PATH": str(down_dir)}):
            self.assertEqual(tailscale_summary(),
                             {"present": True, "connected": False,
                              "ip": None})
        missing_dir = _make_fake_bin(self.root, None)
        with mock.patch.dict(os.environ, {"PATH": str(missing_dir)}):
            self.assertEqual(tailscale_summary(),
                             {"present": False, "connected": False,
                              "ip": None})
        bad_dir = _make_fake_bin(self.root, "badjson")
        with mock.patch.dict(os.environ, {"PATH": str(bad_dir)}):
            self.assertEqual(tailscale_summary(),
                             {"present": True, "connected": False,
                              "ip": None})


# --------------------------------------------------------------------------
# Doctor (direct)
# --------------------------------------------------------------------------

class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ws = self.root / "ws"
        self.ws.mkdir()
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        self.token_file = self.data_dir / "token"
        _write_token_file(self.token_file)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, bin_dir: Path, **kwargs) -> dict:
        with mock.patch.dict(os.environ, {"PATH": str(bin_dir)}), \
                mock.patch.object(shutil, "disk_usage", _roomy_disk):
            return run_doctor(
                workspace=kwargs.get("workspace", self.ws),
                data_dir=kwargs.get("data_dir", self.data_dir),
                token_file=kwargs.get("token_file", self.token_file),
                bind=kwargs.get("bind", "127.0.0.1"),
                port=kwargs.get("port", 8765),
                bind_mode=kwargs.get("bind_mode", "explicit"))

    def _by_name(self, report: dict) -> dict:
        return {c["name"]: c for c in report["checks"]}

    def test_all_pass_and_no_token_leak(self):
        bin_dir = _make_fake_bin(self.root, "up")
        report = self._run(bin_dir)
        self.assertEqual(report["overall"], "pass")
        self.assertEqual(report["summary"]["fail"], 0)
        self.assertEqual(report["summary"]["warn"], 0)
        blob = json.dumps(report)
        self.assertNotIn(TEST_TOKEN, blob)
        self.assertNotIn("tskey", blob)
        self.assertNotIn("FAKEKEY", blob)
        checks = self._by_name(report)
        self.assertEqual(checks["tailscale_connected"]["status"], "pass")
        self.assertEqual(checks["token_permissions"]["status"], "pass")
        self.assertEqual(checks["systemd_unit"]["status"], "pass")
        self.assertEqual(checks["bind_mode"]["status"], "pass")
        self.assertEqual(checks["disk_free"]["status"], "pass")

    def test_disk_floor_thresholds(self):
        bin_dir = _make_fake_bin(self.root, "up")
        cases = [(512 * (1 << 20), "fail"), (3 * (1 << 30), "warn"),
                 (10 * (1 << 30), "pass")]
        for free, expected in cases:
            def fake(_path, _free=free):
                return _DiskUsage(total=100 * (1 << 30),
                                  used=100 * (1 << 30) - _free, free=_free)
            with mock.patch.dict(os.environ, {"PATH": str(bin_dir)}), \
                    mock.patch.object(shutil, "disk_usage", fake):
                report = run_doctor(
                    workspace=self.ws, data_dir=self.data_dir,
                    token_file=self.token_file, bind="127.0.0.1",
                    port=8765, bind_mode="explicit")
            self.assertEqual(
                self._by_name(report)["disk_free"]["status"], expected)

    def test_token_insecure_permissions_fail(self):
        _write_token_file(self.token_file, mode=0o644)
        bin_dir = _make_fake_bin(self.root, "up")
        report = self._run(bin_dir)
        checks = self._by_name(report)
        self.assertEqual(checks["token_permissions"]["status"], "fail")
        self.assertIn("chmod 600", checks["token_permissions"]["fix_hint"])
        self.assertNotIn(TEST_TOKEN, json.dumps(report))

    def test_token_missing_fails(self):
        self.token_file.unlink()
        bin_dir = _make_fake_bin(self.root, "up")
        report = self._run(bin_dir)
        self.assertEqual(
            self._by_name(report)["token_permissions"]["status"], "fail")

    def test_tailscale_missing_fails_tailscale_checks(self):
        bin_dir = _make_fake_bin(self.root, None)
        report = self._run(bin_dir)
        checks = self._by_name(report)
        self.assertEqual(checks["tailscale_present"]["status"], "fail")
        self.assertEqual(checks["tailscale_connected"]["status"], "fail")
        self.assertEqual(report["overall"], "fail")

    def test_tailscale_down_fails_connected_check(self):
        bin_dir = _make_fake_bin(self.root, "down")
        report = self._run(bin_dir)
        checks = self._by_name(report)
        self.assertEqual(checks["tailscale_present"]["status"], "pass")
        self.assertEqual(checks["tailscale_connected"]["status"], "fail")

    def test_missing_tool_warns_with_install_hint(self):
        bin_dir = _make_fake_bin(self.root, "up", tools=("ffmpeg",))
        report = self._run(bin_dir)
        checks = self._by_name(report)
        self.assertEqual(checks["tool_melt"]["status"], "warn")
        self.assertIn("pacman -S mlt", checks["tool_melt"]["fix_hint"])
        self.assertEqual(checks["tool_ffmpeg"]["status"], "pass")

    def test_workspace_missing_fails(self):
        bin_dir = _make_fake_bin(self.root, "up")
        report = self._run(bin_dir, workspace=self.root / "no-such-ws")
        self.assertEqual(
            self._by_name(report)["workspace"]["status"], "fail")

    def test_systemd_disabled_warns(self):
        bin_dir = _make_fake_bin(self.root, "up", unit_enabled="disabled",
                                 unit_active="inactive")
        report = self._run(bin_dir)
        check = self._by_name(report)["systemd_unit"]
        self.assertEqual(check["status"], "warn")
        self.assertIn("disabled", check["detail"])

    def test_systemd_missing_warns(self):
        bin_dir = _make_fake_bin(self.root, "up", with_systemctl=False)
        report = self._run(bin_dir)
        self.assertEqual(
            self._by_name(report)["systemd_unit"]["status"], "warn")

    def test_bind_cgnat_passes_and_public_fails(self):
        bin_dir = _make_fake_bin(self.root, "up")
        report = self._run(bin_dir, bind="100.74.209.52",
                           bind_mode="tailscale")
        self.assertEqual(
            self._by_name(report)["bind_mode"]["status"], "pass")
        report = self._run(bin_dir, bind="192.168.1.5")
        self.assertEqual(
            self._by_name(report)["bind_mode"]["status"], "fail")
        self.assertEqual(report["overall"], "fail")


# --------------------------------------------------------------------------
# HTTP endpoints
# --------------------------------------------------------------------------

class HttpTailscaleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws = root / "ws"
        self.ws.mkdir()
        self.data_dir = root / "data"
        self.data_dir.mkdir()
        token_file = self.data_dir / "token"
        _write_token_file(token_file)
        self.config = SimpleNamespace(
            bind="127.0.0.1", port=0, workspace=self.ws,
            data_dir=self.data_dir, token_file=token_file,
            audit_path=self.data_dir / "audit.jsonl",
            db_path=self.data_dir / "memory.db",
            read_only_paths=[], extra_patterns=[],
            allow_systemctl_manage=False,
        )
        self.up_dir = _make_fake_bin(root, "up")
        self.down_dir = _make_fake_bin(root, "down")
        self.missing_dir = _make_fake_bin(root, None)
        self._old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(self.up_dir)
        self.server = make_server(self.config, TEST_TOKEN)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        handler_cls = self.server.RequestHandlerClass
        try:
            handler_cls.jobs.kill_all()
            handler_cls.jobs.close()
        except Exception:
            pass
        try:
            handler_cls.workflows.interrupt_all()
            handler_cls.workflows.close()
        except Exception:
            pass
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        try:
            handler_cls.store.close()
        except Exception:
            pass
        os.environ["PATH"] = self._old_path
        self.tmp.cleanup()

    def request(self, method, path, payload=None, token="<redacted>"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
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

    def test_version_stays_0_4_0(self):
        self.assertEqual(VERSION, "0.4.0")
        self.assertEqual(__version__, "0.4.0")

    def test_system_tailscale_ok_and_secret_free(self):
        status, body = self.request("POST", "/system/tailscale",
                                    payload={}, token="correct")
        self.assertEqual(status, 200)
        self.assertTrue(body["connected"])
        self.assertEqual(body["self_ip"], "100.74.209.52")
        self.assertEqual(body["peer_count"], 2)
        blob = json.dumps(body)
        self.assertNotIn("tskey", blob)
        self.assertNotIn("FAKEKEY", blob)
        self.assertNotIn("secret-peer", blob)
        self.assertNotIn(TEST_TOKEN, blob)
        audit = (self.data_dir / "audit.jsonl").read_text()
        self.assertNotIn(TEST_TOKEN, audit)
        self.assertNotIn("tskey", audit)

    def test_system_tailscale_requires_auth(self):
        status, _body = self.request("POST", "/system/tailscale",
                                     payload={}, token="none")
        self.assertEqual(status, 401)

    def test_system_tailscale_501_when_missing(self):
        os.environ["PATH"] = str(self.missing_dir)
        try:
            status, body = self.request("POST", "/system/tailscale",
                                        payload={}, token="correct")
        finally:
            os.environ["PATH"] = str(self.up_dir)
        self.assertEqual(status, 501)
        self.assertIn("tailscale", body["error"])

    def test_system_tailscale_502_when_down(self):
        os.environ["PATH"] = str(self.down_dir)
        try:
            status, body = self.request("POST", "/system/tailscale",
                                        payload={}, token="correct")
        finally:
            os.environ["PATH"] = str(self.up_dir)
        self.assertEqual(status, 502)
        self.assertIn("tailscale", body["error"].lower())

    def test_system_doctor_ok_and_secret_free(self):
        with mock.patch.object(shutil, "disk_usage", _roomy_disk):
            status, body = self.request("POST", "/system/doctor",
                                        payload={}, token="correct")
        self.assertEqual(status, 200)
        self.assertEqual(body["overall"], "pass")
        self.assertIn("checks", body)
        self.assertIn("summary", body)
        blob = json.dumps(body)
        self.assertNotIn(TEST_TOKEN, blob)
        self.assertNotIn("tskey", blob)
        audit = (self.data_dir / "audit.jsonl").read_text()
        self.assertNotIn(TEST_TOKEN, audit)

    def test_system_doctor_requires_auth(self):
        status, _body = self.request("POST", "/system/doctor",
                                     payload={}, token="none")
        self.assertEqual(status, 401)

    def test_health_reports_tailscale_fields(self):
        status, body = self.request("GET", "/health", token="none")
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], "0.4.0")
        self.assertEqual(body["tailscale"],
                         {"present": True, "connected": True,
                          "ip": "100.74.209.52"})
        os.environ["PATH"] = str(self.missing_dir)
        try:
            status, body = self.request("GET", "/health", token="none")
        finally:
            os.environ["PATH"] = str(self.up_dir)
        self.assertEqual(status, 200)
        self.assertEqual(body["tailscale"],
                         {"present": False, "connected": False,
                          "ip": None})


# --------------------------------------------------------------------------
# Helper scripts
# --------------------------------------------------------------------------

class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.data_dir = self.home / ".local" / "share" / "hydra"
        self.data_dir.mkdir(parents=True)
        self.token_file = self.data_dir / "token"
        _write_token_file(self.token_file)
        self.ws = self.root / "ws"
        self.ws.mkdir()
        self.bin_dir = _make_fake_bin(self.root, "up")
        self.orig_path = os.environ.get("PATH", "")

    def tearDown(self):
        self.tmp.cleanup()

    def _env(self, **extra) -> dict:
        env = dict(os.environ)
        env["PATH"] = str(self.bin_dir) + os.pathsep + self.orig_path
        env["HOME"] = str(self.home)
        env["HYDRA_DATA_DIR"] = str(self.data_dir)
        env["HYDRA_TOKEN_FILE"] = str(self.token_file)
        env["HYDRA_WORKSPACE"] = str(self.ws)
        env["HYDRA_PORT"] = "8765"
        env.update(extra)
        return env

    def _run_script(self, name: str, args=(), **env_extra):
        return subprocess.run(
            ["bash", str(REPO_ROOT / "scripts" / name), *args],
            env=self._env(**env_extra), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, timeout=30,
            cwd=str(REPO_ROOT))

    def test_pair_card_fingerprint_prefix_only(self):
        result = self._run_script("pair.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = hashlib.sha256(
            self.token_file.read_bytes()).hexdigest()[:12]
        self.assertIn(expected, result.stdout)
        self.assertNotIn(TEST_TOKEN, result.stdout)
        self.assertNotIn(TEST_TOKEN, result.stderr)
        self.assertIn("100.74.209.52", result.stdout)
        self.assertIn("http://100.74.209.52:8765", result.stdout)
        # The fingerprint line must show exactly 12 hex chars.
        match = re.search(
            r"fingerprint \(SHA-256, first 12 chars\): ([0-9a-f]+)",
            result.stdout)
        self.assertIsNotNone(match)
        self.assertEqual(len(match.group(1)), 12)

    def test_pair_card_missing_token(self):
        self.token_file.unlink()
        result = self._run_script("pair.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MISSING", result.stdout)
        self.assertNotIn(TEST_TOKEN, result.stdout)

    def test_setup_script_sudo_only_in_printed_text(self):
        text = (REPO_ROOT / "scripts" / "setup-tailscale.sh").read_text()
        for line in text.splitlines():
            if "sudo" in line:
                stripped = line.strip()
                self.assertTrue(
                    stripped.startswith("echo ") or
                    stripped.startswith("echo\"") or
                    stripped.startswith("printf "),
                    f"sudo outside printed text: {line!r}")
        # A full upgrade is never suggested as a command to run: the
        # only -Syu mention is a DO-NOT warning.
        for line in text.splitlines():
            if "-Syu" in line:
                self.assertIn("NOT", line,
                              f"-Syu suggested without NOT: {line!r}")
        self.assertIn("sudo pacman -S tailscale", text)

    def test_setup_script_run_prints_only_no_enable(self):
        log = self.root / "systemctl.log"
        result = self._run_script(
            "setup-tailscale.sh", FAKE_SYSTEMCTL_LOG=str(log))
        self.assertEqual(result.returncode, 0, result.stderr)
        # Tailscale is installed in this fixture, so the install line
        # is (correctly) skipped and the up/start lines are printed.
        self.assertNotIn("sudo pacman -S tailscale", result.stdout)
        self.assertIn("sudo tailscale up", result.stdout)
        # No flag => systemctl must never be asked to enable/start.
        if log.exists():
            self.assertNotIn("enable", log.read_text())
            self.assertNotIn("daemon-reload", log.read_text())

    def test_setup_script_missing_tailscale_prints_install(self):
        no_ts_bin = self.root / "bin-no-ts"
        no_ts_bin.mkdir()
        _write_fake_systemctl(no_ts_bin)
        # PATH without any tailscale: the real one lives in
        # /opt/hatch/bin on the VM, so plain /usr/bin:/bin is clean.
        path = str(no_ts_bin) + os.pathsep + "/usr/bin" + os.pathsep + "/bin"
        result = self._run_script("setup-tailscale.sh", PATH=path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("tailscale: NOT FOUND", result.stdout)
        self.assertIn("sudo pacman -S tailscale", result.stdout)

    def test_setup_script_enable_autostart_runs_user_unit_only(self):
        unit_dir = self.home / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        (unit_dir / "hydra.service").write_text("[Unit]\n")
        log = self.root / "systemctl.log"
        result = self._run_script(
            "setup-tailscale.sh", ["--enable-autostart"],
            FAKE_SYSTEMCTL_LOG=str(log))
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = log.read_text()
        self.assertIn("--user daemon-reload", calls)
        self.assertIn("--user enable --now hydra", calls)
        self.assertNotIn("sudo", calls)

    def test_setup_script_enable_autostart_without_unit_refuses(self):
        log = self.root / "systemctl.log"
        result = self._run_script(
            "setup-tailscale.sh", ["--enable-autostart"],
            FAKE_SYSTEMCTL_LOG=str(log))
        self.assertEqual(result.returncode, 1)
        self.assertIn("NOT installed", result.stdout)
        if log.exists():
            self.assertNotIn("enable", log.read_text())

    def test_setup_script_low_disk_warns_against_full_upgrade(self):
        result = self._run_script(
            "setup-tailscale.sh", HYDRA_DISK_FREE_BYTES="100")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("LOW", result.stdout)
        self.assertIn("Do NOT run: sudo pacman -Syu", result.stdout)

    def test_kill_switch_without_confirm_keeps_files(self):
        marker = self.data_dir / "jobs.db"
        marker.write_text("x")
        log = self.root / "systemctl.log"
        result = self._run_script(
            "kill-switch.sh", FAKE_SYSTEMCTL_LOG=str(log))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(marker.exists())
        self.assertTrue(self.token_file.exists())
        self.assertIn(str(self.data_dir), result.stdout)
        self.assertIn("NOT deleted", result.stdout)
        self.assertIn("--user stop hydra", log.read_text())
        self.assertIn("sudo tailscale down", result.stdout)

    def test_kill_switch_confirm_deletes_data_dir(self):
        marker = self.data_dir / "jobs.db"
        marker.write_text("x")
        result = self._run_script("kill-switch.sh", ["--confirm"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.data_dir.exists())
        self.assertIn(str(self.data_dir), result.stdout)

    def test_kill_switch_refuses_unsafe_data_dir(self):
        result = self._run_script(
            "kill-switch.sh", ["--confirm"], HYDRA_DATA_DIR="/")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REFUSING", result.stdout + result.stderr)

    def test_kill_switch_script_sudo_only_in_printed_text(self):
        text = (REPO_ROOT / "scripts" / "kill-switch.sh").read_text()
        for line in text.splitlines():
            if "sudo" in line:
                stripped = line.strip()
                self.assertTrue(
                    stripped.startswith("echo "),
                    f"sudo outside printed text: {line!r}")


if __name__ == "__main__":
    unittest.main()
