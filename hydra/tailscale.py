"""Tailscale status helpers (HYDRA v0.4, Tailscale-auto).

Read-only, local-CLI-only probes. This module never changes anything:
it runs the ``tailscale`` command-line tool with fixed argument lists
(``tailscale ip -4``, ``tailscale status --json``), parses the output,
and returns a small, secret-free summary. It never runs ``tailscale
up``/``down``/``login``, never touches keys, and makes no network calls
of its own — the ``tailscale`` CLI talks to the local ``tailscaled``
over its local socket, exactly as Baji's own terminal would.

What this module deliberately does NOT do (see docs/TAILSCALE_AUTO.md):
no relay, no Cloudflare, no polling, no HTTPS termination, no public
bind, no credential storage, no auto-update. The per-session token
handoff is unchanged: Mikey never stores the token.

Secrecy rule: ``tailscale status --json`` contains node keys and full
peer records. This module extracts ONLY: backend state, the local
hostname, the local Tailscale IPv4, the daemon version string if
present, and a peer COUNT. Keys and peer identities never leave this
module — callers (endpoints, doctor, logs) can only ever see the
summary dict returned here.
"""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import subprocess

_TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")
_DEFAULT_TIMEOUT = 10.0
_HEALTH_TIMEOUT = 5.0


class TailscaleError(RuntimeError):
    """The tailscale CLI ran but Tailscale is unusable (down, not
    logged in, bad output). Maps to HTTP 502 at the daemon layer:
    the local backend answered, but is not in a working state."""


class TailscaleUnavailableError(RuntimeError):
    """The ``tailscale`` binary is not installed. Maps to HTTP 501
    naming the Arch package."""


def _scrubbed_env() -> dict[str, str]:
    """Minimal child environment: PATH + HOME + LC_ALL only, so no
    HYDRA_* variable (or anything else sensitive) ever reaches the
    tailscale CLI."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", ""),
        "LC_ALL": "C",
    }


def _run_tailscale(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    """Run one fixed tailscale command. Raises TailscaleUnavailableError
    when the binary is missing; timeouts surface as TailscaleError."""
    exe = shutil.which("tailscale")
    if not exe:
        raise TailscaleUnavailableError(
            "tailscale is not installed; install it with: "
            "sudo pacman -S tailscale (see docs/TAILSCALE_AUTO.md)")
    try:
        return subprocess.run(
            [exe] + args, env=_scrubbed_env(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise TailscaleError(
            f"tailscale {' '.join(args)} timed out after {timeout}s; "
            "is tailscaled stuck? Check with: tailscale status") from exc
    except OSError as exc:
        raise TailscaleError(f"could not run tailscale: {exc}") from exc


def tailscale_present() -> bool:
    """True when a ``tailscale`` binary is on PATH. Says nothing
    about whether tailscaled is running or logged in."""
    return shutil.which("tailscale") is not None


def resolve_tailscale_ip(timeout: float = _DEFAULT_TIMEOUT) -> str:
    """Return this machine's Tailscale IPv4 address.

    Runs ``tailscale ip -4`` and validates the first non-empty line
    as an IPv4 address inside 100.64.0.0/10. Fails closed: any other
    outcome (binary missing, tailscaled down, empty output, an
    address outside the CGNAT range) raises, with a message that
    says what Baji should do. The daemon's ``--bind tailscale``
    mode calls this at startup and refuses to start on failure —
    it never falls back to another bind address silently.
    """
    result = _run_tailscale(["ip", "-4"], timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise TailscaleError(
            "tailscale is not usable (tailscaled down or not logged "
            f"in): {detail[:200] or 'tailscale ip -4 failed'}; "
            "bring it up yourself with: sudo tailscale up "
            "(see docs/TAILSCALE_AUTO.md)")
    for line in result.stdout.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        try:
            ip = ipaddress.ip_address(candidate)
        except ValueError:
            raise TailscaleError(
                f"tailscale ip -4 returned a non-IP value: "
                f"{candidate[:64]!r}") from None
        if not isinstance(ip, ipaddress.IPv4Address) or ip not in _TAILSCALE_NET:
            raise TailscaleError(
                f"tailscale ip -4 returned {candidate}, which is not "
                "a Tailscale IPv4 address (100.64.0.0/10); refusing "
                "to bind to it")
        return candidate
    raise TailscaleError(
        "tailscale ip -4 returned no address; Tailscale is probably "
        "down or not logged in — bring it up yourself with: "
        "sudo tailscale up (see docs/TAILSCALE_AUTO.md)")


def parse_tailscale_status_json(raw: str) -> dict:
    """Parse ``tailscale status --json`` output into the secret-free
    summary. Only five facts are extracted; everything else in the
    JSON (node keys, peer records, routes, DNS config) is dropped.

    ``connected`` is True only when BackendState is "Running" AND
    the local node reports at least one Tailscale IP. Any other
    backend state (NeedsLogin, Stopped, NoState, ...) is a normal,
    non-error answer with ``connected: false`` — the caller can
    see exactly what state Tailscale is in.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TailscaleError(
            f"tailscale status --json returned unparseable output: {exc}"
        ) from None
    if not isinstance(data, dict):
        raise TailscaleError(
            "tailscale status --json returned a non-object document")
    backend_state = data.get("BackendState")
    if backend_state is not None and not isinstance(backend_state, str):
        backend_state = None
    self_node = data.get("Self")
    if not isinstance(self_node, dict):
        self_node = {}
    hostname = self_node.get("HostName")
    if not isinstance(hostname, str) or not hostname:
        hostname = None
    self_ip = None
    ips = self_node.get("TailscaleIPs")
    if isinstance(ips, list):
        for candidate in ips:
            if isinstance(candidate, str) and candidate:
                try:
                    ip = ipaddress.ip_address(candidate)
                except ValueError:
                    continue
                if isinstance(ip, ipaddress.IPv4Address) and ip in _TAILSCALE_NET:
                    self_ip = candidate
                    break
    version = data.get("Version")
    if not isinstance(version, str) or not version:
        version = None
    peers = data.get("Peer")
    peer_count = len(peers) if isinstance(peers, dict) else 0
    connected = backend_state == "Running" and self_ip is not None
    return {
        "connected": connected,
        "backend_state": backend_state,
        "self_hostname": hostname,
        "self_ip": self_ip,
        "version": version,
        "peer_count": peer_count,
    }


def tailscale_status(timeout: float = _DEFAULT_TIMEOUT) -> dict:
    """Run ``tailscale status --json`` and return the secret-free
    summary from :func:`parse_tailscale_status_json`.

    Raises TailscaleUnavailableError when the binary is missing and
    TailscaleError when tailscaled is down or the output is
    unusable. A parseable status in a non-Running state is NOT an
    error: it returns ``connected: false`` with the real state.
    """
    result = _run_tailscale(["status", "--json"], timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise TailscaleError(
            "tailscale status failed (tailscaled down or not "
            f"reachable): {detail[:200] or 'no detail'}; check with: "
            "tailscale status (see docs/TAILSCALE_AUTO.md)")
    return parse_tailscale_status_json(result.stdout)


def tailscale_summary(timeout: float = _HEALTH_TIMEOUT) -> dict:
    """Best-effort summary for GET /health. NEVER raises: every
    failure degrades to ``present``/``connected`` booleans and a
    null IP, so health checks stay cheap and honest.

    Shape: ``{"present": bool, "connected": bool, "ip": str|None}``.
    ``ip`` is the local Tailscale IPv4 when connected, else None.
    No secrets, no keys, no peer data — the IP is the only
    address-like value, and it is already the address callers use
    to reach the daemon.
    """
    if not tailscale_present():
        return {"present": False, "connected": False, "ip": None}
    try:
        status = tailscale_status(timeout=timeout)
    except (TailscaleUnavailableError, TailscaleError):
        return {"present": True, "connected": False, "ip": None}
    except Exception:  # never let /health fail on a probe
        return {"present": True, "connected": False, "ip": None}
    return {"present": True, "connected": bool(status["connected"]),
            "ip": status["self_ip"] if status["connected"] else None}
