"""HYDRA video pipeline v0.4: headless Kdenlive/MLT editing + melt rendering.

The correct path for video work on Baji's laptop is HEADLESS, not GUI
clicking: Kdenlive project files are MLT XML, which code can generate,
and ``melt`` (MLT's own renderer, the same engine Kdenlive uses)
renders that XML without ever opening a window. No mouse, no
screenshots, no GUI automation anywhere in this module — consistent
with Baji's background-only rule.

What this module does (all dumb execution, no model, no network):

* ``probe_media`` — run ``ffprobe`` (fixed argv, no shell) and parse
  duration / streams / resolution / fps / codec.
* Timeline model — tracks of clips (media in/out trims, still images,
  pango text titles), crossfade/mix transitions between tracks,
  per-clip volume, and an optional SRT/VTT subtitle sidecar. The
  model is validated (no overlapping clips on a track, times inside
  the probed media, sane caps) before anything is written.
* ``build_mlt_xml`` — generate valid MLT XML with stdlib
  ``xml.etree``: ``<profile>``, producers, one playlist per track,
  and a ``main`` tractor with a multitrack, transitions, and
  per-entry volume filters. The file is renderable by ``melt`` and
  openable in Kdenlive as an MLT project. It is NOT a full
  ``.kdenlive`` project (no bin metadata, no Kdenlive effects
  catalogue); Kdenlive round-trip fidelity is unverified — see
  ``docs/VIDEO_SETUP.md``.
* ``render_melt`` — run ``melt`` with a fixed consumer argument list
  (libx264 + AAC). Missing ``melt`` is an honest 501 at the daemon
  layer, never a fake success.
* Subtitles are NOT burned inside MLT: after ``melt`` renders, an
  optional ffmpeg post-pass burns an SRT/VTT sidecar into the final
  file. If the burn fails, the unburned render is kept and the
  failure is reported — a good render is never thrown away silently.
* ``validate_output`` — re-probe the render and check it has a video
  stream, a nonzero size, and a duration within tolerance of the
  timeline. ``extract_frames`` pulls PNG preview frames via ffmpeg.
* Asset manifest — every asset gets a SHA256 + probe result +
  source/license note in ``manifest.json``. The daemon NEVER
  downloads anything: URLs in the manifest are provenance notes
  only. Media arrives via /write, /exec, or /jobs (Mikey supplies
  each URL explicitly); there is no auto-scraper here, and
  copyrighted movie/game clips must never be fetched.
* Disk guard — renders are refused when free space is below a
  conservative estimate (output bitrate x duration x safety factor
  + headroom). Baji's root partition is tight; a render must never
  be the thing that fills it.

References that shaped this module (reference only — nothing is
copied, and none of these is a runtime dependency; HYDRA stays
stdlib-only):

* kdenlive-mcp by mhbxyz (Linux, Python 3.11+, Kdenlive + melt +
  ffprobe): the typed timeline model, ffprobe diagnostics, preset
  rendering, and render verification ideas.
* cli-anything-kdenlive (JSON project format + MLT XML generation
  for Kdenlive/melt): the JSON-spec -> MLT XML -> melt shape.
* mlt-python (pure-Python MLT XML library): the producer/playlist/
  tractor element structure. HYDRA reimplements the small subset it
  needs with ``xml.etree`` instead of depending on it.
* MLT's own XML documentation (mltframework.org/docs/mltxml):
  producers first, playlists per track, tractor + multitrack +
  transitions, clock time values (``HH:MM:SS.mmm``).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from .security import resolve_inside

# ---------------------------------------------------------------------------
# Profiles / presets
# ---------------------------------------------------------------------------
# video_bitrate_bps is a CONSERVATIVE planning figure for the disk
# guard (CRF renders vary); it is not passed to melt, which renders
# with CRF 18. The guard multiplies by SAFETY_FACTOR, so the estimate
# stays on the safe side of real output sizes.

PROFILES: dict[str, dict] = {
    "1080p30": {
        "width": 1920, "height": 1080, "fps_num": 30, "fps_den": 1,
        "display_aspect": (16, 9), "video_bitrate_bps": 12_000_000,
        "audio_bitrate_bps": 192_000,
        "description": "HD 1920x1080 30p",
    },
    "1080p30-vertical": {
        "width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1,
        "display_aspect": (9, 16), "video_bitrate_bps": 10_000_000,
        "audio_bitrate_bps": 192_000,
        "description": "Vertical HD 1080x1920 30p",
    },
    "720p30": {
        "width": 1280, "height": 720, "fps_num": 30, "fps_den": 1,
        "display_aspect": (16, 9), "video_bitrate_bps": 6_000_000,
        "audio_bitrate_bps": 160_000,
        "description": "HD 1280x720 30p",
    },
    "1080p25": {
        "width": 1920, "height": 1080, "fps_num": 25, "fps_den": 1,
        "display_aspect": (16, 9), "video_bitrate_bps": 12_000_000,
        "audio_bitrate_bps": 192_000,
        "description": "HD 1920x1080 25p",
    },
}

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"}
SUBTITLE_EXTENSIONS = {".srt", ".vtt"}

MAX_TRACKS = 8
MAX_CLIPS_PER_TRACK = 200
MAX_CLIPS_TOTAL = 500
MAX_TITLE_CHARS = 500
MAX_PREVIEW_FRAMES = 10
PROBE_TIMEOUT_SECONDS = 30
FRAME_TIMEOUT_SECONDS = 60
DISK_SAFETY_FACTOR = 2.0
DISK_HEADROOM_BYTES = 100 * 1024 * 1024  # 100 MB on top of the estimate
DURATION_TOLERANCE_SECONDS = 0.5
DURATION_TOLERANCE_FRACTION = 0.02  # 2% of expected duration
DEFAULT_IMAGE_DURATION = 5.0
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class VideoError(ValueError):
    """Bad input or bad media: the caller's request cannot be honoured.

    Maps to HTTP 400 at the daemon layer. Subclassed by
    TransientVideoError for failures worth retrying (render timeouts,
    melt crashes) — the workflow runner retries only those.
    """

    transient = False


class TransientVideoError(VideoError):
    """A failure that might succeed on retry (render crash/timeout)."""

    transient = True


class VideoUnavailableError(RuntimeError):
    """A required tool (ffprobe/melt/ffmpeg) is not installed.

    Maps to HTTP 501 naming the missing tool and the Arch package.
    """


class DiskSpaceError(RuntimeError):
    """The disk guard refused a render: not enough free space.

    Maps to HTTP 507 at the daemon layer.
    """


# ---------------------------------------------------------------------------
# Tool presence
# ---------------------------------------------------------------------------

def video_tools() -> dict[str, bool]:
    """Presence booleans for the video toolchain. No version guessing:
    a missing binary is False, a present one is True, nothing more is
    claimed. ``kdenlive`` itself is only needed to OPEN projects by
    hand; headless render needs ``melt`` + ``ffmpeg`` + ``ffprobe``.
    """
    return {name: shutil.which(name) is not None
            for name in ("kdenlive", "melt", "ffmpeg", "ffprobe")}


def _require_tool(name: str, package: str, override: str | None = None) -> str:
    """Resolve a tool binary or raise VideoUnavailableError naming the
    Arch package. ``override`` (tests) is used verbatim when given.
    """
    if override:
        return override
    exe = shutil.which(name)
    if not exe:
        raise VideoUnavailableError(
            f"{name} is not installed; install it with: "
            f"sudo pacman -S {package} (see docs/VIDEO_SETUP.md)")
    return exe


def _scrubbed_env() -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": os.environ.get("HOME", ""),
           "LC_ALL": "C"}
    return env


# ---------------------------------------------------------------------------
# Time handling (MLT clock values: HH:MM:SS.mmm)
# ---------------------------------------------------------------------------

def seconds_to_timecode(seconds: float) -> str:
    """Format seconds as an MLT clock value. A radix in the last
    component makes MLT parse it as clock time, not SMPTE frames."""
    if seconds < 0:
        raise VideoError("negative time value")
    total_ms = int(round(seconds * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(float(value))


def _as_seconds(value: object, field: str) -> float:
    if not _is_number(value):
        raise VideoError(f"{field} must be a number (seconds)")
    seconds = float(value)  # type: ignore[arg-type]
    if seconds < 0:
        raise VideoError(f"{field} must be >= 0")
    return seconds


# ---------------------------------------------------------------------------
# ffprobe
# ---------------------------------------------------------------------------

def parse_fps(rate: object) -> float | None:
    """Parse an ffprobe frame-rate string like "30/1" or "30000/1001".

    Returns None for "0/0", missing, or unparseable values — never
    raises, because a missing fps must not fail a whole probe.
    """
    if not isinstance(rate, str) or "/" not in rate:
        return None
    num, _, den = rate.partition("/")
    try:
        numerator, denominator = float(num), float(den)
    except ValueError:
        return None
    if denominator == 0 or numerator <= 0:
        return None
    return numerator / denominator


def parse_ffprobe_json(data: dict) -> dict:
    """Parse ffprobe -show_format -show_streams JSON into HYDRA's
    probe shape. Raises VideoError when the JSON has no usable
    streams at all (not a media file, or a truncated probe)."""
    if not isinstance(data, dict):
        raise VideoError("ffprobe output was not a JSON object")
    streams_in = data.get("streams")
    if not isinstance(streams_in, list) or not streams_in:
        raise VideoError("ffprobe found no streams in this file")
    fmt = data.get("format") or {}
    duration: float | None = None
    if isinstance(fmt, dict) and fmt.get("duration") is not None:
        try:
            duration = float(fmt["duration"])
        except (TypeError, ValueError):
            duration = None
    streams: list[dict] = []
    video: dict | None = None
    audio: dict | None = None
    for stream in streams_in:
        if not isinstance(stream, dict):
            continue
        codec_type = stream.get("codec_type")
        entry: dict = {
            "index": stream.get("index"),
            "codec_type": codec_type,
            "codec_name": stream.get("codec_name"),
        }
        if codec_type == "video":
            entry["width"] = stream.get("width")
            entry["height"] = stream.get("height")
            entry["fps"] = parse_fps(stream.get("avg_frame_rate")) \
                or parse_fps(stream.get("r_frame_rate"))
            if video is None:
                video = entry
        elif codec_type == "audio":
            entry["sample_rate"] = stream.get("sample_rate")
            entry["channels"] = stream.get("channels")
            if audio is None:
                audio = entry
        streams.append(entry)
    if video is None and audio is None:
        raise VideoError("ffprobe found no video or audio stream")
    # Still images report no duration: duration stays None and the
    # caller treats the file as a still (clip duration comes from
    # the timeline spec). A video file with no duration is suspect
    # but not fatal here; render validation catches empty output.
    size_bytes: int | None = None
    if isinstance(fmt, dict) and fmt.get("size") is not None:
        try:
            size_bytes = int(fmt["size"])
        except (TypeError, ValueError):
            size_bytes = None
    return {
        "duration": duration,
        "width": video.get("width") if video else None,
        "height": video.get("height") if video else None,
        "fps": video.get("fps") if video else None,
        "video_codec": video.get("codec_name") if video else None,
        "audio_codec": audio.get("codec_name") if audio else None,
        "has_video": video is not None,
        "has_audio": audio is not None,
        "streams": streams,
        "size_bytes": size_bytes,
        "format_name": fmt.get("format_name") if isinstance(fmt, dict) else None,
    }


def probe_media(path: Path, ffprobe_exe: str | None = None,
                timeout: int = PROBE_TIMEOUT_SECONDS) -> dict:
    """Probe one media file with ffprobe (fixed argv, no shell).

    Raises VideoUnavailableError when ffprobe is missing, VideoError
    when the file cannot be probed. The caller must already have
    jail-checked ``path``; this function only checks it is a file.
    """
    exe = _require_tool("ffprobe", "ffmpeg", ffprobe_exe)
    if not path.is_file():
        raise VideoError(f"not a file: {path}")
    try:
        proc = subprocess.run(
            [exe, "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout, env=_scrubbed_env(),
            errors="replace")
    except subprocess.TimeoutExpired as exc:
        raise VideoError(f"ffprobe timed out probing {path.name}") from exc
    except OSError as exc:
        raise VideoError(f"ffprobe could not run: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()[:300]
        raise VideoError(
            f"ffprobe failed on {path.name} (exit {proc.returncode})"
            + (f": {detail}" if detail else ""))
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise VideoError(f"ffprobe returned invalid JSON: {exc}") from exc
    probe = parse_ffprobe_json(data)
    probe["path"] = str(path)
    return probe


# ---------------------------------------------------------------------------
# Hashing + manifest
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    """Streaming SHA256 hex digest (never loads the whole file)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(workspace: Path, entries: list[dict],
                   ffprobe_exe: str | None = None,
                   probe_if_available: bool = True) -> dict:
    """Build the asset manifest for a project.

    Each entry needs ``path`` (workspace-relative); ``source``,
    ``source_url`` and ``license`` are provenance notes recorded
    verbatim and NEVER fetched. Every asset must exist inside the
    workspace jail. When ffprobe is available each asset is probed
    and the result stored; probe failures are recorded as
    ``probe_error`` on that asset (the timeline step decides whether
    that is fatal) instead of aborting the whole manifest.
    """
    ffprobe_present = probe_if_available and (
        ffprobe_exe is not None or shutil.which("ffprobe") is not None)
    assets: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise VideoError("manifest entries must be objects")
        rel = str(entry.get("path", ""))
        resolved = resolve_inside(workspace, rel)  # raises on escape
        if not resolved.is_file():
            raise VideoError(f"manifest asset is not a file: {rel!r}")
        probe: dict | None = None
        probe_error: str | None = None
        if ffprobe_present:
            try:
                probe = probe_media(resolved, ffprobe_exe=ffprobe_exe)
                probe.pop("path", None)
            except VideoError as exc:
                probe_error = str(exc)
        assets.append({
            "path": rel,
            "sha256": sha256_file(resolved),
            "size_bytes": resolved.stat().st_size,
            "source": str(entry.get("source", "")),
            "source_url": str(entry.get("source_url", "")),
            "license": str(entry.get("license", "")),
            "probe": probe,
            "probe_error": probe_error,
        })
    return {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "assets": assets,
    }


# ---------------------------------------------------------------------------
# Timeline model + validation
# ---------------------------------------------------------------------------

def validate_project_id(name: object) -> str:
    """Project ids become directory names under ``video/``: strict
    charset, no separators, no leading dot, no ``..``. Raises
    VideoError on anything else."""
    if not isinstance(name, str) or not name:
        raise VideoError("project name must be a non-empty string")
    if len(name) > 64 or not _PROJECT_ID_RE.match(name) \
            or name in (".", "..") or ".." in name:
        raise VideoError(
            "project name must match [A-Za-z0-9][A-Za-z0-9._-]* "
            "(max 64 chars, no path separators)")
    return name


def _media_kind(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    raise VideoError(
        f"unsupported media extension {ext!r} for {path.name}; "
        "supported: video, image, audio files")


def _volume_to_db(volume: float) -> float:
    """Linear gain -> dB for MLT's volume filter ``level`` property
    (Kdenlive semantics: 0 dB = unchanged, floor at -60 dB)."""
    if volume <= 0:
        return -60.0
    return round(20.0 * math.log10(volume), 2)


def validate_timeline(spec: dict, workspace: Path,
                      probes: dict[str, dict | None] | None = None) -> dict:
    """Validate a timeline spec and return the normalized model.

    ``spec`` shape (see docs/ARCHITECTURE.md for the full example)::

        {"name": "my-video", "profile": "1080p30",
         "tracks": [{"type": "video", "clips": [
             {"asset": "clips/a.mp4", "in": 0, "out": 5, "start": 0},
             {"kind": "title", "text": "Hello", "duration": 3}]}],
         "transitions": [{"type": "crossfade", "a_track": 0,
                          "b_track": 1, "start": 4.5, "duration": 1.0}],
         "subtitle": "subs/captions.srt"}

    ``start`` defaults to appending after the previous clip on the
    same track. Media clips need ``in``/``out`` (or ``duration`` +
    ``in``); when a probe is known, ``out`` beyond the media duration
    is refused. Image/title clips need ``duration`` (images default
    to 5s when omitted). Overlapping clips on one track are refused
    (gaps are fine — they become blanks). ``probes`` maps
    workspace-relative asset paths to probe dicts (or None when
    unprobed); when a probe is absent, explicit times are required.
    """
    if not isinstance(spec, dict):
        raise VideoError("timeline spec must be an object")
    name = validate_project_id(spec.get("name"))
    profile_name = str(spec.get("profile", ""))
    profile = PROFILES.get(profile_name)
    if profile is None:
        raise VideoError(
            f"unknown profile {profile_name!r}; "
            f"known profiles: {', '.join(sorted(PROFILES))}")
    tracks_in = spec.get("tracks")
    if not isinstance(tracks_in, list) or not tracks_in:
        raise VideoError("timeline needs at least one track")
    if len(tracks_in) > MAX_TRACKS:
        raise VideoError(f"too many tracks (max {MAX_TRACKS})")
    probes = probes or {}

    tracks: list[dict] = []
    total_clips = 0
    timeline_duration = 0.0
    for track_index, track_in in enumerate(tracks_in):
        if not isinstance(track_in, dict):
            raise VideoError(f"track {track_index} must be an object")
        track_type = str(track_in.get("type", ""))
        if track_type not in ("video", "audio"):
            raise VideoError(
                f"track {track_index} type must be video or audio")
        clips_in = track_in.get("clips")
        if not isinstance(clips_in, list):
            raise VideoError(f"track {track_index} clips must be a list")
        if len(clips_in) > MAX_CLIPS_PER_TRACK:
            raise VideoError(
                f"track {track_index} has too many clips "
                f"(max {MAX_CLIPS_PER_TRACK})")
        clips: list[dict] = []
        cursor = 0.0  # append position when start is omitted
        for clip_index, clip_in in enumerate(clips_in):
            clip = _validate_clip(
                clip_in, track_index, clip_index, track_type,
                workspace, probes)
            if "start" in clip_in and clip_in["start"] is not None:
                clip["start"] = _as_seconds(clip_in["start"], "start")
            else:
                clip["start"] = cursor
            if clip["start"] < cursor - 1e-9:
                raise VideoError(
                    f"track {track_index} clip {clip_index} starts at "
                    f"{clip['start']}s but the previous clip ends at "
                    f"{cursor}s: overlapping clips on one track are "
                    "refused (put overlapping clips on adjacent tracks "
                    "and add a transition)")
            cursor = clip["start"] + clip["duration"]
            timeline_duration = max(timeline_duration, cursor)
            clips.append(clip)
        total_clips += len(clips)
        tracks.append({"type": track_type, "clips": clips})
    if total_clips == 0:
        raise VideoError("timeline needs at least one clip")
    if total_clips > MAX_CLIPS_TOTAL:
        raise VideoError(f"too many clips in total (max {MAX_CLIPS_TOTAL})")

    transitions = _validate_transitions(
        spec.get("transitions") or [], len(tracks), timeline_duration)

    subtitle_rel: str | None = None
    subtitle_abs: str | None = None
    if spec.get("subtitle"):
        subtitle_rel = str(spec["subtitle"])
        resolved = resolve_inside(workspace, subtitle_rel)
        if resolved.suffix.lower() not in SUBTITLE_EXTENSIONS:
            raise VideoError("subtitle file must be .srt or .vtt")
        if not resolved.is_file():
            raise VideoError(f"subtitle file not found: {subtitle_rel!r}")
        subtitle_abs = str(resolved)

    return {
        "name": name,
        "profile": profile_name,
        "width": profile["width"],
        "height": profile["height"],
        "fps_num": profile["fps_num"],
        "fps_den": profile["fps_den"],
        "display_aspect": list(profile["display_aspect"]),
        "duration": timeline_duration,
        "tracks": tracks,
        "transitions": transitions,
        "subtitle": subtitle_rel,
        "subtitle_abs": subtitle_abs,
    }


def _validate_clip(clip_in: object, track_index: int, clip_index: int,
                   track_type: str, workspace: Path,
                   probes: dict[str, dict | None]) -> dict:
    where = f"track {track_index} clip {clip_index}"
    if not isinstance(clip_in, dict):
        raise VideoError(f"{where} must be an object")
    volume = clip_in.get("volume", 1.0)
    if not _is_number(volume) or not 0.0 <= float(volume) <= 2.0:  # type: ignore[arg-type]
        raise VideoError(f"{where} volume must be a number in [0, 2]")
    volume = float(volume)  # type: ignore[arg-type]

    if clip_in.get("kind") == "title":
        if track_type != "video":
            raise VideoError(f"{where}: titles need a video track")
        text = clip_in.get("text", "")
        if not isinstance(text, str) or not text.strip():
            raise VideoError(f"{where}: title text must be non-empty")
        if len(text) > MAX_TITLE_CHARS:
            raise VideoError(
                f"{where}: title text too long (max {MAX_TITLE_CHARS})")
        duration = _as_seconds(clip_in.get("duration"), f"{where} duration")
        if duration <= 0:
            raise VideoError(f"{where}: title duration must be > 0")
        return {
            "kind": "title", "text": text,
            "font": str(clip_in.get("font", "Sans")),
            "font_size": _title_int(clip_in.get("font_size", 48), where),
            "color": str(clip_in.get("color", "#FFFFFF")),
            "in": 0.0, "out": duration, "duration": duration,
            "volume": volume, "asset": None, "resource": None,
        }

    asset_rel = str(clip_in.get("asset", ""))
    if not asset_rel:
        raise VideoError(
            f"{where}: needs an asset path or kind=title")
    resolved = resolve_inside(workspace, asset_rel)  # raises on escape
    if not resolved.is_file():
        raise VideoError(f"{where}: asset not found: {asset_rel!r}")
    kind = _media_kind(resolved)
    if track_type == "audio" and kind == "image":
        raise VideoError(f"{where}: images need a video track")
    if track_type == "audio" and kind == "video":
        # Video on an audio track is allowed: only its audio is used
        # (the track hides video). No special handling needed.
        pass
    probe = probes.get(asset_rel)
    media_duration = probe.get("duration") if probe else None

    in_s = _as_seconds(clip_in.get("in", 0), f"{where} in")
    if "out" in clip_in and clip_in["out"] is not None:
        out_s = _as_seconds(clip_in["out"], f"{where} out")
    elif "duration" in clip_in and clip_in["duration"] is not None:
        out_s = in_s + _as_seconds(clip_in["duration"], f"{where} duration")
    elif kind == "image":
        out_s = in_s + DEFAULT_IMAGE_DURATION
    elif media_duration is not None:
        out_s = float(media_duration)
    else:
        raise VideoError(
            f"{where}: give explicit in/out (or duration); the asset "
            "could not be probed and no duration is known")
    if out_s <= in_s:
        raise VideoError(f"{where}: out must be greater than in")
    if media_duration is not None and out_s > float(media_duration) + 0.1:
        raise VideoError(
            f"{where}: out ({out_s}s) exceeds the media duration "
            f"({media_duration}s)")
    return {
        "kind": kind, "asset": asset_rel, "resource": str(resolved),
        "in": in_s, "out": out_s, "duration": out_s - in_s,
        "volume": volume, "text": None,
    }


def _title_int(value: object, where: str) -> int:
    if isinstance(value, bool):
        raise VideoError(f"{where}: font_size must be an integer")
    try:
        size = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise VideoError(f"{where}: font_size must be an integer") from None
    if not 8 <= size <= 400:
        raise VideoError(f"{where}: font_size must be in [8, 400]")
    return size


def _validate_transitions(transitions_in: object, track_count: int,
                          timeline_duration: float) -> list[dict]:
    if not isinstance(transitions_in, list):
        raise VideoError("transitions must be a list")
    transitions: list[dict] = []
    for index, item in enumerate(transitions_in):
        where = f"transition {index}"
        if not isinstance(item, dict):
            raise VideoError(f"{where} must be an object")
        kind = str(item.get("type", ""))
        if kind not in ("crossfade", "mix"):
            raise VideoError(
                f"{where}: type must be crossfade or mix")
        try:
            a_track = int(item.get("a_track"))  # type: ignore[arg-type]
            b_track = int(item.get("b_track"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise VideoError(
                f"{where}: a_track/b_track must be integers") from None
        if not 0 <= a_track < track_count or not 0 <= b_track < track_count:
            raise VideoError(f"{where}: track index out of range")
        if a_track == b_track:
            raise VideoError(
                f"{where}: a_track and b_track must differ")
        start = _as_seconds(item.get("start"), f"{where} start")
        duration = _as_seconds(item.get("duration"), f"{where} duration")
        if duration <= 0:
            raise VideoError(f"{where}: duration must be > 0")
        if start + duration > timeline_duration + 1e-9:
            raise VideoError(
                f"{where}: transition runs past the timeline end")
        transitions.append({
            "type": kind, "a_track": a_track, "b_track": b_track,
            "start": start, "duration": duration,
        })
    return transitions


# ---------------------------------------------------------------------------
# MLT XML generation
# ---------------------------------------------------------------------------

def _property(parent: ET.Element, name: str, value: str) -> None:
    prop = ET.SubElement(parent, "property", {"name": name})
    prop.text = value


def _clock_in_out(element: ET.Element, in_s: float, out_s: float) -> None:
    element.set("in", seconds_to_timecode(in_s))
    element.set("out", seconds_to_timecode(out_s))


def build_mlt_xml(timeline: dict, title: str | None = None) -> str:
    """Generate MLT XML for a normalized timeline (see
    ``validate_timeline``). Producers first, one playlist per track,
    then the ``main`` tractor with multitrack + transitions. Gaps on
    a track become ``<blank>`` entries so playlist positions match
    timeline positions exactly. Per-clip volume becomes a ``volume``
    filter attached to the playlist entry (dB conversion, see
    ``_volume_to_db``). A ``crossfade`` transition emits both a
    ``luma`` (video) and a ``mix`` (audio) transition over the same
    range, matching melt's ``-mixer luma -mixer mix`` pairing; a
    ``mix`` transition emits only the audio one.

    Crossfade rendering against real melt/Kdenlive is UNVERIFIED
    (no melt on the build VM): the luma transition carries no
    resource file, and the first laptop render must confirm the
    dissolve visually. See docs/VIDEO_SETUP.md.
    """
    fps_num = timeline["fps_num"]
    fps_den = timeline["fps_den"]
    root = ET.Element("mlt", {
        "LC_NUMERIC": "C",
        "version": "7.0.0",
        "title": title or timeline["name"],
    })
    dar_num, dar_den = timeline["display_aspect"]
    ET.SubElement(root, "profile", {
        "description": PROFILES[timeline["profile"]]["description"],
        "width": str(timeline["width"]),
        "height": str(timeline["height"]),
        "progressive": "1",
        "sample_aspect_num": "1",
        "sample_aspect_den": "1",
        "display_aspect_num": str(dar_num),
        "display_aspect_den": str(dar_den),
        "frame_rate_num": str(fps_num),
        "frame_rate_den": str(fps_den),
        "colorspace": "709",
    })

    # Producers, in track/clip order, with stable ids.
    producer_ids: dict[tuple[int, int], str] = {}
    counter = 0
    for track_index, track in enumerate(timeline["tracks"]):
        for clip_index, clip in enumerate(track["clips"]):
            pid = f"producer{counter}"
            counter += 1
            producer_ids[(track_index, clip_index)] = pid
            producer = ET.SubElement(root, "producer", {"id": pid})
            _clock_in_out(producer, clip["in"], clip["out"])
            if clip["kind"] == "title":
                _property(producer, "mlt_service", "pango")
                _property(producer, "text", clip["text"])
                _property(producer, "font",
                          f"{clip['font']} {clip['font_size']}")
                _property(producer, "fgcolour", clip["color"])
                _property(producer, "bgcolour", "0x00000000")
                _property(producer, "pad", "20")
                _property(producer, "halign", "1")
                _property(producer, "valign", "1")
            elif clip["kind"] == "image":
                _property(producer, "resource", clip["resource"])
                _property(producer, "mlt_service", "qimage")
            else:
                _property(producer, "resource", clip["resource"])
                _property(producer, "mlt_service", "avformat")

    # One playlist per track; blanks fill gaps so entries land at
    # their timeline positions.
    filter_counter = 0
    for track_index, track in enumerate(timeline["tracks"]):
        playlist = ET.SubElement(root, "playlist",
                                 {"id": f"playlist{track_index}"})
        cursor = 0.0
        for clip_index, clip in enumerate(track["clips"]):
            gap = clip["start"] - cursor
            if gap > 1e-9:
                ET.SubElement(playlist, "blank",
                              {"length": seconds_to_timecode(gap)})
            entry = ET.SubElement(
                playlist, "entry",
                {"producer": producer_ids[(track_index, clip_index)]})
            _clock_in_out(entry, clip["in"], clip["out"])
            if clip["volume"] != 1.0:
                filt = ET.SubElement(
                    entry, "filter", {"id": f"filter{filter_counter}"})
                filter_counter += 1
                _clock_in_out(filt, clip["in"], clip["out"])
                _property(filt, "mlt_service", "volume")
                _property(filt, "window", "75")
                _property(filt, "max_gain", "20dB")
                _property(filt, "level", str(_volume_to_db(clip["volume"])))
            cursor = clip["start"] + clip["duration"]

    # Main tractor: multitrack + transitions.
    tractor = ET.SubElement(root, "tractor", {"id": "main",
                                              "title": timeline["name"]})
    _clock_in_out(tractor, 0.0, timeline["duration"])
    _property(tractor, "mlt_service", "tractor")
    multitrack = ET.SubElement(tractor, "multitrack")
    for track_index, track in enumerate(timeline["tracks"]):
        attrs = {"producer": f"playlist{track_index}"}
        if track["type"] == "audio":
            attrs["hide"] = "video"
        ET.SubElement(multitrack, "track", attrs)
    transition_counter = 0
    for transition in timeline["transitions"]:
        services = ("luma", "mix") if transition["type"] == "crossfade" \
            else ("mix",)
        for service in services:
            trans = ET.SubElement(
                tractor, "transition",
                {"id": f"transition{transition_counter}"})
            transition_counter += 1
            _clock_in_out(trans, transition["start"],
                          transition["start"] + transition["duration"])
            _property(trans, "a_track", str(transition["a_track"]))
            _property(trans, "b_track", str(transition["b_track"]))
            _property(trans, "mlt_service", service)

    ET.indent(root, space="  ")
    xml_body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + xml_body + "\n"


# ---------------------------------------------------------------------------
# Disk guard + render + validate + previews
# ---------------------------------------------------------------------------

def estimate_output_bytes(duration_seconds: float, profile_name: str) -> int:
    """Conservative render-size estimate: planning bitrate x duration
    x safety factor + headroom. Used ONLY by the disk guard; real
    CRF output is usually smaller."""
    profile = PROFILES[profile_name]
    total_bps = profile["video_bitrate_bps"] + profile["audio_bitrate_bps"]
    return int(duration_seconds * total_bps / 8 * DISK_SAFETY_FACTOR) \
        + DISK_HEADROOM_BYTES


def check_disk_space(directory: Path, needed_bytes: int) -> dict:
    """Compare free space on ``directory``'s filesystem with the
    estimate. Returns {ok, free_bytes, needed_bytes, path}. Never
    raises for an unreadable filesystem: free_bytes None means
    unknown, and the caller must refuse (fail closed)."""
    try:
        usage = shutil.disk_usage(str(directory))
        free = usage.free
    except OSError:
        free = None
    ok = free is not None and free >= needed_bytes
    return {"ok": ok, "free_bytes": free, "needed_bytes": needed_bytes,
            "path": str(directory)}


def build_melt_argv(xml_path: Path, output_path: Path) -> list[str]:
    """Fixed melt consumer arguments. The profile (size/fps) comes
    from the <profile> in the XML; the consumer only fixes codecs.
    No shell, no user-supplied flags: callers cannot inject melt
    options through project data."""
    return ["melt", str(xml_path), "-consumer",
            f"avformat:{output_path}",
            "vcodec=libx264", "preset=medium", "crf=18",
            "acodec=aac", "ab=192k"]


def render_melt(xml_path: Path, output_path: Path,
                timeout_seconds: int,
                melt_exe: str | None = None,
                on_pid: object = None) -> dict:
    """Run melt to render ``xml_path`` to ``output_path``.

    ``on_pid`` (optional callable) receives the melt pid as soon as
    the process spawns, so the workflow layer can record it for
    restart recovery. Raises VideoUnavailableError (no melt),
    TransientVideoError (timeout / nonzero exit — worth one retry
    by the workflow runner), or VideoError (no output produced).
    The melt stderr tail is returned on success for diagnostics and
    included in failure messages (bounded, never the whole log).
    """
    import signal  # local: only this function signals process groups

    exe = _require_tool("melt", "mlt", melt_exe)
    argv = build_melt_argv(xml_path, output_path)
    argv[0] = exe
    output_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL, start_new_session=True,
        text=True, errors="replace", env=_scrubbed_env())
    if callable(on_pid):
        try:
            on_pid(proc.pid)
        except Exception:
            pass
    try:
        _stdout, stderr = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        _stdout, stderr = proc.communicate()
        raise TransientVideoError(
            f"melt render timed out after {timeout_seconds}s") from None
    tail = (stderr or "")[-2000:]
    if proc.returncode != 0:
        raise TransientVideoError(
            f"melt failed (exit {proc.returncode})"
            + (f": {tail.strip()[-500:]}" if tail.strip() else ""))
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise VideoError(
            "melt exited 0 but produced no output file "
            f"(looked for {output_path.name})")
    return {"output_path": str(output_path),
            "size_bytes": output_path.stat().st_size,
            "returncode": proc.returncode,
            "stderr_tail": tail}


def _escape_subtitle_filter_path(path: str) -> str:
    """Escape a path for ffmpeg's subtitles filter argument (colons,
    quotes, backslashes and brackets are filter syntax)."""
    return (path.replace("\\", "\\\\").replace(":", "\\:")
            .replace("'", "\\'").replace("[", "\\[").replace("]", "\\]"))


def burn_subtitles(video_path: Path, subtitle_path: Path,
                   output_path: Path, timeout_seconds: int = 3600,
                   ffmpeg_exe: str | None = None) -> dict:
    """Burn an SRT/VTT sidecar into ``video_path`` with ffmpeg,
    writing ``output_path`` (video re-encoded, audio copied).
    Raises VideoUnavailableError (no ffmpeg) or VideoError (ffmpeg
    failed). The caller decides whether a burn failure fails the
    workflow; this function only reports honestly.
    """
    exe = _require_tool("ffmpeg", "ffmpeg", ffmpeg_exe)
    filter_arg = "subtitles=" + _escape_subtitle_filter_path(
        str(subtitle_path))
    argv = [exe, "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(video_path), "-vf", filter_arg,
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-c:a", "copy", str(output_path)]
    try:
        proc = subprocess.run(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout_seconds, env=_scrubbed_env(),
            stdin=subprocess.DEVNULL, errors="replace")
    except subprocess.TimeoutExpired as exc:
        raise VideoError(
            f"subtitle burn timed out after {timeout_seconds}s") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()[-500:]
        raise VideoError(
            f"ffmpeg subtitle burn failed (exit {proc.returncode})"
            + (f": {detail}" if detail else ""))
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise VideoError("subtitle burn produced no output file")
    return {"output_path": str(output_path),
            "size_bytes": output_path.stat().st_size,
            "subtitles_burned": True}


def validate_output(output_path: Path, expected_duration: float,
                    ffprobe_exe: str | None = None) -> dict:
    """Re-probe a render and check it is a real video of about the
    expected length. Tolerance is max(0.5s, 2% of expected) to
    absorb MLT frame rounding. Raises VideoError on any failure,
    with the measured values in the message — never a silent pass.
    """
    if not output_path.is_file():
        raise VideoError(f"render output missing: {output_path.name}")
    size = output_path.stat().st_size
    if size == 0:
        raise VideoError("render output is empty (0 bytes)")
    probe = probe_media(output_path, ffprobe_exe=ffprobe_exe)
    if not probe["has_video"]:
        raise VideoError("render output has no video stream")
    measured = probe["duration"]
    tolerance = max(DURATION_TOLERANCE_SECONDS,
                    DURATION_TOLERANCE_FRACTION * expected_duration)
    if measured is not None and abs(measured - expected_duration) > tolerance:
        raise VideoError(
            f"render duration {measured:.2f}s differs from expected "
            f"{expected_duration:.2f}s by more than {tolerance:.2f}s")
    return {"output_path": str(output_path), "size_bytes": size,
            "duration": measured, "expected_duration": expected_duration,
            "tolerance_seconds": tolerance,
            "width": probe["width"], "height": probe["height"],
            "fps": probe["fps"], "video_codec": probe["video_codec"],
            "audio_codec": probe["audio_codec"],
            "has_audio": probe["has_audio"]}


def extract_frames(video_path: Path, timestamps: list[float],
                   output_dir: Path,
                   ffmpeg_exe: str | None = None) -> list[dict]:
    """Extract one PNG per timestamp via ffmpeg (fixed argv, no
    shell). Frames are written as ``frame_0001.png`` ... in
    ``output_dir`` (created if needed). Each PNG is verified by
    magic bytes. Raises VideoUnavailableError (no ffmpeg) or
    VideoError (bad timestamp / ffmpeg failure / bad PNG)."""
    exe = _require_tool("ffmpeg", "ffmpeg", ffmpeg_exe)
    if not timestamps:
        raise VideoError("at least one timestamp is required")
    if len(timestamps) > MAX_PREVIEW_FRAMES:
        raise VideoError(
            f"too many timestamps (max {MAX_PREVIEW_FRAMES})")
    if not video_path.is_file():
        raise VideoError(f"not a file: {video_path}")
    output_dir.mkdir(parents=True, exist_ok=True)
    frames: list[dict] = []
    for index, ts in enumerate(timestamps, start=1):
        if not _is_number(ts) or float(ts) < 0:  # type: ignore[arg-type]
            raise VideoError(f"timestamp {ts!r} must be >= 0")
        out = output_dir / f"frame_{index:04d}.png"
        argv = [exe, "-hide_banner", "-loglevel", "error", "-y",
                "-ss", str(float(ts)), "-i", str(video_path),
                "-frames:v", "1", "-q:v", "3", str(out)]
        try:
            proc = subprocess.run(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=FRAME_TIMEOUT_SECONDS,
                env=_scrubbed_env(), stdin=subprocess.DEVNULL,
                errors="replace")
        except subprocess.TimeoutExpired as exc:
            raise VideoError(
                f"ffmpeg frame extraction timed out at {ts}s") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or "").strip()[-300:]
            raise VideoError(
                f"ffmpeg frame extraction failed at {ts}s "
                f"(exit {proc.returncode})"
                + (f": {detail}" if detail else ""))
        data = out.read_bytes() if out.is_file() else b""
        if not data.startswith(PNG_MAGIC):
            raise VideoError(
                f"frame at {ts}s is not a valid PNG "
                f"(ffmpeg produced {len(data)} bytes)")
        frames.append({"timestamp": float(ts), "path": str(out),
                       "bytes": len(data)})
    return frames


# ---------------------------------------------------------------------------
# Project creation (writes timeline.json + manifest.json + project.mlt)
# ---------------------------------------------------------------------------

def project_dir_for(workspace: Path, project_id: str) -> Path:
    return resolve_inside(workspace, f"video/{project_id}")


def create_project(workspace: Path, spec: dict,
                   overwrite: bool = False,
                   ffprobe_exe: str | None = None) -> dict:
    """Validate a timeline spec, probe its assets, and write the
    project directory (``video/<name>/``) with ``timeline.json``,
    ``manifest.json`` and ``project.mlt``. Raises VideoError on bad
    specs, FileExistsError when the project already exists and
    ``overwrite`` is false. Nothing is rendered here — rendering is
    the workflow's job (see hydra/workflows.py).
    """
    if not isinstance(spec, dict):
        raise VideoError("project spec must be an object")
    name = validate_project_id(spec.get("name"))
    project_dir = project_dir_for(workspace, name)
    timeline_path = project_dir / "timeline.json"
    if timeline_path.exists() and not overwrite:
        raise FileExistsError(
            f"project {name!r} already exists; pass overwrite=true "
            "to replace its timeline/manifest/XML (assets are kept)")

    # Collect asset entries: explicit "assets" (provenance) plus any
    # clip asset not already listed (provenance empty).
    asset_entries: dict[str, dict] = {}
    for entry in spec.get("assets") or []:
        if not isinstance(entry, dict) or not entry.get("path"):
            raise VideoError("asset entries need a path")
        asset_entries[str(entry["path"])] = dict(entry)
    for track in spec.get("tracks") or []:
        if not isinstance(track, dict):
            continue
        for clip in track.get("clips") or []:
            if isinstance(clip, dict) and clip.get("asset"):
                asset_entries.setdefault(str(clip["asset"]),
                                         {"path": str(clip["asset"])})

    manifest = build_manifest(
        workspace, list(asset_entries.values()),
        ffprobe_exe=ffprobe_exe)
    probes = {a["path"]: a["probe"] for a in manifest["assets"]}

    timeline = validate_timeline(spec, workspace, probes=probes)
    timeline["manifest_assets"] = len(manifest["assets"])
    xml_text = build_mlt_xml(timeline)

    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "timeline.json").write_text(
        json.dumps(timeline, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    (project_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    (project_dir / "project.mlt").write_text(xml_text, encoding="utf-8")
    return {
        "project_id": name,
        "dir": f"video/{name}",
        "timeline_path": f"video/{name}/timeline.json",
        "manifest_path": f"video/{name}/manifest.json",
        "mlt_path": f"video/{name}/project.mlt",
        "profile": timeline["profile"],
        "duration": timeline["duration"],
        "track_count": len(timeline["tracks"]),
        "clip_count": sum(len(t["clips"]) for t in timeline["tracks"]),
        "asset_count": len(manifest["assets"]),
        "subtitle": timeline["subtitle"],
    }


def load_project(workspace: Path, project_id: str) -> dict:
    """Load a project's timeline.json. Raises FileNotFoundError when
    the project does not exist, VideoError when its files are
    unreadable. The project id is validated (no path tricks)."""
    name = validate_project_id(project_id)
    timeline_path = project_dir_for(workspace, name) / "timeline.json"
    if not timeline_path.is_file():
        raise FileNotFoundError(f"no such video project: {name!r}")
    try:
        timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VideoError(f"project {name!r} timeline is unreadable: {exc}"
                         ) from exc
    if not isinstance(timeline, dict):
        raise VideoError(f"project {name!r} timeline is not an object")
    return timeline


def list_projects(workspace: Path) -> list[dict]:
    """Projects under ``video/`` that have a timeline.json. A
    directory with unreadable JSON is listed with an error note
    instead of being silently skipped."""
    video_root = workspace / "video"
    projects: list[dict] = []
    if not video_root.is_dir():
        return projects
    for child in sorted(video_root.iterdir(), key=lambda p: p.name):
        if not child.is_dir():
            continue
        timeline_path = child / "timeline.json"
        if not timeline_path.is_file():
            continue
        try:
            timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
            projects.append({
                "project_id": child.name,
                "dir": f"video/{child.name}",
                "profile": timeline.get("profile"),
                "duration": timeline.get("duration"),
                "track_count": len(timeline.get("tracks", [])),
                "clip_count": sum(
                    len(t.get("clips", []))
                    for t in timeline.get("tracks", [])),
                "has_mlt": (child / "project.mlt").is_file(),
                "has_manifest": (child / "manifest.json").is_file(),
            })
        except (OSError, json.JSONDecodeError, AttributeError):
            projects.append({
                "project_id": child.name, "dir": f"video/{child.name}",
                "error": "timeline.json is unreadable",
            })
    return projects
