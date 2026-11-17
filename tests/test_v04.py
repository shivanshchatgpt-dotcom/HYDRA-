"""HYDRA v0.4 tests (stdlib unittest only).

Run from the repo root:
    python3 -m unittest discover -s tests -v

Covers every v0.4 addition: ffprobe parsing (fake ffprobe JSON and
a fake ffprobe binary), timeline validation, MLT XML generation
(parsed back with ElementTree: clip order, durations, blanks,
transitions, volume filters, titles), the asset manifest, the disk
guard, workflow checkpoint/resume across a simulated restart, retry
with backoff, render-queue exclusivity, and the HTTP endpoints
including 401s, path-escape rejection, and honest 501s when melt is
missing. One end-to-end render test runs the real workflow against
a fake ``melt`` (which renders with real ffmpeg) to prove the
plumbing; real melt/Kdenlive rendering on Baji's laptop is NOT
covered here and stays on the docs/VIDEO_SETUP.md checklist.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hydra import __version__  # noqa: E402
from hydra.daemon import VERSION, make_server  # noqa: E402
from hydra.video import (  # noqa: E402
    DiskSpaceError,
    TransientVideoError,
    VideoError,
    VideoUnavailableError,
    build_manifest,
    build_melt_argv,
    build_mlt_xml,
    check_disk_space,
    create_project,
    estimate_output_bytes,
    extract_frames,
    list_projects,
    load_project,
    parse_ffprobe_json,
    parse_fps,
    probe_media,
    seconds_to_timecode,
    sha256_file,
    validate_project_id,
    validate_timeline,
    validate_output,
    video_tools,
)
from hydra.workflows import STEPS, WorkflowError, WorkflowManager  # noqa: E402

TEST_TOKEN = "test-token-" + "x" * 24  # >= 20 chars, test-only
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

FAKE_FFPROBE_SCRIPT = """#!/usr/bin/env python3
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
cfg = {}
try:
    with open(os.path.join(here, "ffprobe_config.json")) as fh:
        cfg = json.load(fh)
except OSError:
    pass
if cfg.get("fail"):
    print("fake ffprobe boom", file=sys.stderr)
    sys.exit(1)
if cfg.get("bad_json"):
    print("this is not json")
    sys.exit(0)
streams = cfg.get("streams")
if streams is None:
    streams = [
        {"index": 0, "codec_type": "video", "codec_name": "h264",
         "width": 320, "height": 240, "avg_frame_rate": "30/1"},
        {"index": 1, "codec_type": "audio", "codec_name": "aac",
         "sample_rate": "48000", "channels": 2},
    ]
print(json.dumps({"format": {"duration": cfg.get("duration", "5.0"),
                             "size": "1234", "format_name": "mp4"},
                  "streams": streams}))
"""

FAKE_FFMPEG_SCRIPT = """#!/usr/bin/env python3
import sys
# extract_frames passes the output path as the last argument.
with open(sys.argv[-1], "wb") as fh:
    fh.write(b"\\x89PNG\\r\\n\\x1a\\n" + b"\\x00" * 64)
"""

FAKE_MELT_SCRIPT = """#!/usr/bin/env python3
import json, os, subprocess, sys, time
here = os.path.dirname(os.path.abspath(__file__))
cfg = {}
try:
    with open(os.path.join(here, "melt_config.json")) as fh:
        cfg = json.load(fh)
except OSError:
    pass
log_path = cfg.get("log")
started = time.monotonic()
if log_path:
    with open(log_path, "a") as fh:
        fh.write(json.dumps({"event": "start", "t": started,
                             "argv": sys.argv[1:]}) + "\\n")
time.sleep(float(cfg.get("sleep", 0)))
output = None
for arg in sys.argv[1:]:
    if arg.startswith("avformat:"):
        output = arg[len("avformat:"):]
if not output:
    print("fake melt: no avformat consumer", file=sys.stderr)
    sys.exit(2)
duration = float(cfg.get("duration", 5))
subprocess.run(
    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
     "-f", "lavfi", "-i",
     f"color=c=black:s=320x240:d={duration}:r=30",
     "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo",
     "-t", str(duration), "-c:v", "libx264", "-pix_fmt", "yuv420p",
     "-c:a", "aac", "-shortest", output],
    check=True)
if log_path:
    with open(log_path, "a") as fh:
        fh.write(json.dumps({"event": "end", "t": time.monotonic()}) + "\\n")
"""


def _write_exe(path: Path, content: str) -> str:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _write_token_file(path: Path, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, (TEST_TOKEN + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, mode)


def _wait_until(predicate, timeout: float = 15.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


def _make_media(path: Path, duration: float = 5.0) -> Path:
    """A real tiny mp4 (black video + silence) via ffmpeg lavfi."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i",
         f"color=c=black:s=320x240:d={duration}:r=30",
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
         "-t", str(duration), "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(path)],
        check=True, timeout=60)
    return path


def _valid_spec(name: str = "demo", clips=None, **extra) -> dict:
    spec = {
        "name": name,
        "profile": "1080p30",
        "tracks": [{"type": "video", "clips": clips if clips is not None
                    else [{"asset": "clips/a.mp4", "in": 0, "out": 5}]}],
    }
    spec.update(extra)
    return spec


# --------------------------------------------------------------------------
# Timecode + ffprobe parsing
# --------------------------------------------------------------------------

class TimecodeTests(unittest.TestCase):
    def test_seconds_to_timecode(self):
        self.assertEqual(seconds_to_timecode(0), "00:00:00.000")
        self.assertEqual(seconds_to_timecode(1.5), "00:00:01.500")
        self.assertEqual(seconds_to_timecode(61.25), "00:01:01.250")
        self.assertEqual(seconds_to_timecode(3661.999), "01:01:01.999")
        self.assertEqual(seconds_to_timecode(0.0004), "00:00:00.000")

    def test_negative_rejected(self):
        with self.assertRaises(VideoError):
            seconds_to_timecode(-1)


class FfprobeParsingTests(unittest.TestCase):
    def test_parse_fps(self):
        self.assertEqual(parse_fps("30/1"), 30.0)
        self.assertAlmostEqual(parse_fps("30000/1001"), 29.97, places=1)
        self.assertIsNone(parse_fps("0/0"))
        self.assertIsNone(parse_fps("bogus"))
        self.assertIsNone(parse_fps(None))
        self.assertIsNone(parse_fps(30))

    def test_parse_video_and_audio(self):
        probe = parse_ffprobe_json({
            "format": {"duration": "12.5", "size": "999",
                       "format_name": "mp4"},
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "h264",
                 "width": 1920, "height": 1080,
                 "avg_frame_rate": "30000/1001"},
                {"index": 1, "codec_type": "audio", "codec_name": "aac",
                 "sample_rate": "48000", "channels": 2},
            ]})
        self.assertEqual(probe["duration"], 12.5)
        self.assertEqual((probe["width"], probe["height"]), (1920, 1080))
        self.assertAlmostEqual(probe["fps"], 29.97, places=1)
        self.assertEqual(probe["video_codec"], "h264")
        self.assertEqual(probe["audio_codec"], "aac")
        self.assertTrue(probe["has_video"] and probe["has_audio"])
        self.assertEqual(len(probe["streams"]), 2)

    def test_parse_still_image_has_no_duration(self):
        probe = parse_ffprobe_json({
            "format": {},
            "streams": [{"index": 0, "codec_type": "video",
                         "codec_name": "png", "width": 800, "height": 600,
                         "avg_frame_rate": "0/0"}]})
        self.assertIsNone(probe["duration"])
        self.assertIsNone(probe["fps"])
        self.assertTrue(probe["has_video"])
        self.assertFalse(probe["has_audio"])

    def test_parse_no_streams_rejected(self):
        with self.assertRaises(VideoError):
            parse_ffprobe_json({"format": {}, "streams": []})
        with self.assertRaises(VideoError):
            parse_ffprobe_json({"format": {}, "streams": [
                {"index": 0, "codec_type": "data",
                 "codec_name": "bin"}]})
        with self.assertRaises(VideoError):
            parse_ffprobe_json("not-a-dict")  # type: ignore[arg-type]


class ProbeMediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bin_dir = Path(self.tmp.name) / "bin"
        self.bin_dir.mkdir()
        self.fake = _write_exe(self.bin_dir / "ffprobe",
                               FAKE_FFPROBE_SCRIPT)
        self.media = Path(self.tmp.name) / "clip.mp4"
        self.media.write_bytes(b"fake-bytes")

    def tearDown(self):
        self.tmp.cleanup()

    def _config(self, **kwargs):
        (self.bin_dir / "ffprobe_config.json").write_text(
            json.dumps(kwargs), encoding="utf-8")

    def test_probe_with_fake_ffprobe(self):
        probe = probe_media(self.media, ffprobe_exe=self.fake)
        self.assertEqual(probe["duration"], 5.0)
        self.assertEqual((probe["width"], probe["height"]), (320, 240))
        self.assertEqual(probe["fps"], 30.0)
        self.assertEqual(probe["path"], str(self.media))

    def test_probe_failure_is_video_error(self):
        self._config(fail=True)
        with self.assertRaises(VideoError):
            probe_media(self.media, ffprobe_exe=self.fake)

    def test_probe_bad_json_is_video_error(self):
        self._config(bad_json=True)
        with self.assertRaises(VideoError):
            probe_media(self.media, ffprobe_exe=self.fake)

    def test_probe_missing_file(self):
        with self.assertRaises(VideoError):
            probe_media(Path(self.tmp.name) / "nope.mp4",
                        ffprobe_exe=self.fake)

    def test_probe_missing_binary_is_unavailable(self):
        with mock.patch("hydra.video.shutil.which", return_value=None):
            with self.assertRaises(VideoUnavailableError) as ctx:
                probe_media(self.media)
        self.assertIn("pacman", str(ctx.exception))

    @unittest.skipUnless(FFPROBE, "ffprobe not installed")
    def test_probe_real_media(self):
        real = _make_media(Path(self.tmp.name) / "real.mp4", duration=2)
        probe = probe_media(real)
        self.assertAlmostEqual(probe["duration"], 2.0, places=1)
        self.assertEqual((probe["width"], probe["height"]), (320, 240))
        self.assertTrue(probe["has_video"] and probe["has_audio"])


# --------------------------------------------------------------------------
# Manifest + hashing
# --------------------------------------------------------------------------

class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name) / "ws"
        (self.ws / "clips").mkdir(parents=True)
        (self.ws / "clips" / "a.mp4").write_bytes(b"aaa")
        (self.ws / "clips" / "b.mp4").write_bytes(b"bbb")

    def tearDown(self):
        self.tmp.cleanup()

    def test_sha256_file(self):
        self.assertEqual(
            sha256_file(self.ws / "clips" / "a.mp4"),
            "9834876dcfb05cb167a5c24953eba58c4ac89b1adf57f28f2f9d09af107ee8f0")

    def test_manifest_hashes_and_provenance(self):
        manifest = build_manifest(self.ws, [
            {"path": "clips/a.mp4", "source": "Pexels",
             "source_url": "https://example.invalid/clip",
             "license": "Pexels License"},
            {"path": "clips/b.mp4"},
        ], probe_if_available=False)
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(len(manifest["assets"]), 2)
        first = manifest["assets"][0]
        self.assertEqual(first["source"], "Pexels")
        # The URL is recorded, never fetched: example.invalid would
        # fail any fetch, yet the manifest builds fine.
        self.assertEqual(first["source_url"],
                         "https://example.invalid/clip")
        self.assertEqual(first["license"], "Pexels License")
        self.assertEqual(first["size_bytes"], 3)
        self.assertIsNone(first["probe"])

    def test_manifest_rejects_escape(self):
        with self.assertRaises(ValueError):
            build_manifest(self.ws, [{"path": "../escape.mp4"}],
                           probe_if_available=False)

    def test_manifest_rejects_missing_file(self):
        with self.assertRaises(VideoError):
            build_manifest(self.ws, [{"path": "clips/nope.mp4"}],
                           probe_if_available=False)

    def test_manifest_records_probe_error_without_aborting(self):
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir()
        fake = _write_exe(bin_dir / "ffprobe", FAKE_FFPROBE_SCRIPT)
        (bin_dir / "ffprobe_config.json").write_text(
            json.dumps({"fail": True}), encoding="utf-8")
        manifest = build_manifest(
            self.ws, [{"path": "clips/a.mp4"}], ffprobe_exe=fake)
        self.assertIsNone(manifest["assets"][0]["probe"])
        self.assertIn("boom", manifest["assets"][0]["probe_error"])


# --------------------------------------------------------------------------
# Timeline validation + MLT XML
# --------------------------------------------------------------------------

class TimelineValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name) / "ws"
        (self.ws / "clips").mkdir(parents=True)
        (self.ws / "clips" / "a.mp4").write_bytes(b"aaa")
        (self.ws / "clips" / "b.mp4").write_bytes(b"bbb")
        (self.ws / "clips" / "pic.png").write_bytes(b"png")
        (self.ws / "subs").mkdir()
        (self.ws / "subs" / "caps.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n")
        self.probes = {
            "clips/a.mp4": {"duration": 10.0},
            "clips/b.mp4": {"duration": 8.0},
            "clips/pic.png": {"duration": None},
        }

    def tearDown(self):
        self.tmp.cleanup()

    def _validate(self, spec):
        return validate_timeline(spec, self.ws, probes=self.probes)

    def test_valid_timeline_normalized(self):
        timeline = self._validate(_valid_spec(clips=[
            {"asset": "clips/a.mp4", "in": 1, "out": 4},
            {"asset": "clips/b.mp4", "in": 0, "out": 2, "start": 5},
        ]))
        self.assertEqual(timeline["duration"], 7.0)
        clips = timeline["tracks"][0]["clips"]
        self.assertEqual(clips[0]["start"], 0.0)  # append default
        self.assertEqual(clips[0]["duration"], 3.0)
        self.assertEqual(clips[1]["start"], 5.0)  # gap -> blank in XML
        self.assertEqual(timeline["profile"], "1080p30")

    def test_overlap_refused(self):
        with self.assertRaises(VideoError):
            self._validate(_valid_spec(clips=[
                {"asset": "clips/a.mp4", "in": 0, "out": 5},
                {"asset": "clips/b.mp4", "in": 0, "out": 2, "start": 4},
            ]))

    def test_unknown_profile_refused(self):
        with self.assertRaises(VideoError):
            self._validate(_valid_spec(profile="4k120"))

    def test_bad_volume_refused(self):
        for volume in (-0.1, 2.5, "loud"):
            with self.assertRaises(VideoError, msg=str(volume)):
                self._validate(_valid_spec(clips=[
                    {"asset": "clips/a.mp4", "in": 0, "out": 2,
                     "volume": volume}]))

    def test_out_beyond_probe_refused(self):
        with self.assertRaises(VideoError):
            self._validate(_valid_spec(clips=[
                {"asset": "clips/a.mp4", "in": 0, "out": 11}]))

    def test_missing_asset_refused(self):
        with self.assertRaises(VideoError):
            self._validate(_valid_spec(clips=[
                {"asset": "clips/nope.mp4", "in": 0, "out": 2}]))

    def test_asset_escape_refused(self):
        with self.assertRaises(ValueError):
            self._validate(_valid_spec(clips=[
                {"asset": "../escape.mp4", "in": 0, "out": 2}]))

    def test_unprobed_clip_needs_explicit_times(self):
        with self.assertRaises(VideoError):
            validate_timeline(_valid_spec(clips=[
                {"asset": "clips/a.mp4", "in": 0}]),
                self.ws, probes={})

    def test_image_defaults_to_5s(self):
        timeline = self._validate(_valid_spec(clips=[
            {"asset": "clips/pic.png"}]))
        self.assertEqual(timeline["tracks"][0]["clips"][0]["duration"], 5.0)

    def test_title_clip(self):
        timeline = self._validate(_valid_spec(clips=[
            {"kind": "title", "text": "Hello", "duration": 3}]))
        clip = timeline["tracks"][0]["clips"][0]
        self.assertEqual(clip["kind"], "title")
        self.assertEqual(clip["text"], "Hello")
        self.assertEqual(timeline["duration"], 3.0)

    def test_title_rules(self):
        with self.assertRaises(VideoError):
            self._validate(_valid_spec(clips=[
                {"kind": "title", "text": "  ", "duration": 3}]))
        with self.assertRaises(VideoError):
            self._validate(_valid_spec(clips=[
                {"kind": "title", "text": "x" * 501, "duration": 3}]))

    def test_transitions_validated(self):
        spec = _valid_spec(
            tracks=[
                {"type": "video", "clips": [
                    {"asset": "clips/a.mp4", "in": 0, "out": 5}]},
                {"type": "video", "clips": [
                    {"asset": "clips/b.mp4", "in": 0, "out": 5,
                     "start": 4}]},
            ],
            transitions=[{"type": "crossfade", "a_track": 0,
                          "b_track": 1, "start": 4, "duration": 1}])
        timeline = self._validate(spec)
        self.assertEqual(len(timeline["transitions"]), 1)
        bad = dict(spec, transitions=[{"type": "wipe", "a_track": 0,
                                       "b_track": 1, "start": 4,
                                       "duration": 1}])
        with self.assertRaises(VideoError):
            self._validate(bad)
        bad = dict(spec, transitions=[{"type": "crossfade", "a_track": 0,
                                       "b_track": 0, "start": 4,
                                       "duration": 1}])
        with self.assertRaises(VideoError):
            self._validate(bad)
        bad = dict(spec, transitions=[{"type": "crossfade", "a_track": 0,
                                       "b_track": 5, "start": 4,
                                       "duration": 1}])
        with self.assertRaises(VideoError):
            self._validate(bad)

    def test_subtitle_validated(self):
        timeline = self._validate(_valid_spec(subtitle="subs/caps.srt"))
        self.assertEqual(timeline["subtitle"], "subs/caps.srt")
        with self.assertRaises(VideoError):
            self._validate(_valid_spec(subtitle="subs/missing.srt"))
        (self.ws / "subs" / "caps.txt").write_text("nope")
        with self.assertRaises(VideoError):
            self._validate(_valid_spec(subtitle="subs/caps.txt"))

    def test_project_id_rules(self):
        self.assertEqual(validate_project_id("my-video_1.2"), "my-video_1.2")
        for bad in ("", "../x", "a/b", "a b", ".hidden", "..", "x" * 65):
            with self.assertRaises(VideoError, msg=bad):
                validate_project_id(bad)


class MltXmlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name) / "ws"
        (self.ws / "clips").mkdir(parents=True)
        (self.ws / "clips" / "a.mp4").write_bytes(b"aaa")
        (self.ws / "clips" / "b.mp4").write_bytes(b"bbb")
        (self.ws / "music.mp3").write_bytes(b"mmm")
        self.probes = {"clips/a.mp4": {"duration": 10.0},
                       "clips/b.mp4": {"duration": 8.0},
                       "music.mp3": {"duration": 30.0}}

    def tearDown(self):
        self.tmp.cleanup()

    def _xml(self, spec):
        timeline = validate_timeline(spec, self.ws, probes=self.probes)
        text = build_mlt_xml(timeline)
        return timeline, ET.fromstring(text)

    def test_profile_and_structure(self):
        _, root = self._xml(_valid_spec())
        self.assertEqual(root.tag, "mlt")
        profile = root.find("profile")
        self.assertEqual(profile.get("width"), "1920")
        self.assertEqual(profile.get("height"), "1080")
        self.assertEqual(profile.get("frame_rate_num"), "30")
        self.assertEqual(profile.get("display_aspect_num"), "16")
        tractor = root.find("tractor[@id='main']")
        self.assertIsNotNone(tractor)
        self.assertEqual(tractor.get("out"), "00:00:05.000")

    def test_vertical_profile(self):
        _, root = self._xml(_valid_spec(profile="1080p30-vertical"))
        profile = root.find("profile")
        self.assertEqual(profile.get("width"), "1080")
        self.assertEqual(profile.get("height"), "1920")
        self.assertEqual(profile.get("display_aspect_num"), "9")

    def test_clip_order_and_durations(self):
        _, root = self._xml(_valid_spec(clips=[
            {"asset": "clips/a.mp4", "in": 1, "out": 4},
            {"asset": "clips/b.mp4", "in": 0, "out": 2, "start": 5},
        ]))
        producers = root.findall("producer")
        self.assertEqual(len(producers), 2)
        resources = [p.find("property[@name='resource']").text
                     for p in producers]
        self.assertTrue(resources[0].endswith("clips/a.mp4"))
        self.assertTrue(resources[1].endswith("clips/b.mp4"))
        self.assertEqual(producers[0].get("in"), "00:00:01.000")
        self.assertEqual(producers[0].get("out"), "00:00:04.000")
        playlist = root.find("playlist[@id='playlist0']")
        children = list(playlist)
        # entry, blank (2s gap), entry
        self.assertEqual([c.tag for c in children],
                         ["entry", "blank", "entry"])
        self.assertEqual(children[1].get("length"), "00:00:02.000")
        self.assertEqual(children[0].get("producer"), "producer0")
        self.assertEqual(children[2].get("producer"), "producer1")

    def test_audio_track_hides_video(self):
        _, root = self._xml(_valid_spec(tracks=[
            {"type": "video", "clips": [
                {"asset": "clips/a.mp4", "in": 0, "out": 5}]},
            {"type": "audio", "clips": [
                {"asset": "music.mp3", "in": 0, "out": 5}]},
        ]))
        tracks = root.findall("tractor/multitrack/track")
        self.assertEqual(len(tracks), 2)
        self.assertIsNone(tracks[0].get("hide"))
        self.assertEqual(tracks[1].get("hide"), "video")

    def test_crossfade_emits_luma_and_mix(self):
        _, root = self._xml(_valid_spec(
            tracks=[
                {"type": "video", "clips": [
                    {"asset": "clips/a.mp4", "in": 0, "out": 5}]},
                {"type": "video", "clips": [
                    {"asset": "clips/b.mp4", "in": 0, "out": 5,
                     "start": 4}]},
            ],
            transitions=[{"type": "crossfade", "a_track": 0,
                          "b_track": 1, "start": 4, "duration": 1}]))
        transitions = root.findall("tractor/transition")
        services = [t.find("property[@name='mlt_service']").text
                    for t in transitions]
        self.assertEqual(services, ["luma", "mix"])
        for trans in transitions:
            self.assertEqual(trans.get("in"), "00:00:04.000")
            self.assertEqual(trans.get("out"), "00:00:05.000")
            self.assertEqual(
                trans.find("property[@name='a_track']").text, "0")
            self.assertEqual(
                trans.find("property[@name='b_track']").text, "1")

    def test_volume_filter_uses_db(self):
        _, root = self._xml(_valid_spec(clips=[
            {"asset": "clips/a.mp4", "in": 0, "out": 5, "volume": 0.5},
            {"asset": "clips/b.mp4", "in": 0, "out": 2, "volume": 1.0},
        ]))
        entries = root.findall("playlist/entry")
        filt = entries[0].find("filter")
        self.assertIsNotNone(filt)
        self.assertEqual(filt.find("property[@name='mlt_service']").text,
                         "volume")
        # 0.5 linear -> about -6.02 dB
        self.assertAlmostEqual(
            float(filt.find("property[@name='level']").text),
            -6.02, places=1)
        self.assertIsNone(entries[1].find("filter"))  # volume 1: no filter

    def test_title_producer_is_pango(self):
        _, root = self._xml(_valid_spec(clips=[
            {"kind": "title", "text": "Hello <world> & friends",
             "duration": 3, "font_size": 64}]))
        producer = root.find("producer")
        self.assertEqual(
            producer.find("property[@name='mlt_service']").text, "pango")
        self.assertEqual(
            producer.find("property[@name='text']").text,
            "Hello <world> & friends")  # XML-escaped, parses back
        self.assertEqual(
            producer.find("property[@name='font']").text, "Sans 64")


# --------------------------------------------------------------------------
# Disk guard, melt argv, output validation, frame extraction
# --------------------------------------------------------------------------

class DiskAndRenderTests(unittest.TestCase):
    def test_estimate_scales_with_duration_and_profile(self):
        short = estimate_output_bytes(60, "1080p30")
        long = estimate_output_bytes(600, "1080p30")
        # 10x duration -> 10x the bitrate part, headroom counted once.
        self.assertEqual(long, short * 10 - 9 * 100 * 1024 * 1024)
        self.assertGreater(long, short * 6)
        vertical = estimate_output_bytes(60, "1080p30-vertical")
        self.assertLess(vertical, short)  # lower planning bitrate
        self.assertGreater(short, 100 * 1024 * 1024)

    def test_check_disk_space(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok = check_disk_space(Path(tmp), 1024)
            self.assertTrue(ok["ok"])
            self.assertGreater(ok["free_bytes"], 0)
            huge = check_disk_space(Path(tmp), 10 ** 18)
            self.assertFalse(huge["ok"])
            self.assertEqual(huge["needed_bytes"], 10 ** 18)

    def test_build_melt_argv_fixed_shape(self):
        argv = build_melt_argv(Path("/ws/video/x/project.mlt"),
                               Path("/ws/video/x/out.mp4"))
        self.assertEqual(argv[0], "melt")
        self.assertEqual(argv[1], "/ws/video/x/project.mlt")
        self.assertEqual(argv[2], "-consumer")
        self.assertEqual(argv[3], "avformat:/ws/video/x/out.mp4")
        self.assertIn("vcodec=libx264", argv)
        self.assertIn("acodec=aac", argv)

    def test_validate_output_with_fake_ffprobe(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            fake = _write_exe(bin_dir / "ffprobe", FAKE_FFPROBE_SCRIPT)
            out = tmp_path / "out.mp4"
            out.write_bytes(b"x" * 100)
            (bin_dir / "ffprobe_config.json").write_text(
                json.dumps({"duration": "5.1"}), encoding="utf-8")
            result = validate_output(out, 5.0, ffprobe_exe=fake)
            self.assertEqual(result["duration"], 5.1)
            self.assertEqual(result["width"], 320)
            (bin_dir / "ffprobe_config.json").write_text(
                json.dumps({"duration": "9.0"}), encoding="utf-8")
            with self.assertRaises(VideoError):
                validate_output(out, 5.0, ffprobe_exe=fake)
            (bin_dir / "ffprobe_config.json").write_text(
                json.dumps({"streams": [
                    {"index": 0, "codec_type": "audio",
                     "codec_name": "aac"}]}), encoding="utf-8")
            with self.assertRaises(VideoError):
                validate_output(out, 5.0, ffprobe_exe=fake)

    def test_extract_frames_with_fake_ffmpeg(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            fake = _write_exe(bin_dir / "ffmpeg", FAKE_FFMPEG_SCRIPT)
            video = tmp_path / "v.mp4"
            video.write_bytes(b"fake")
            frames = extract_frames(video, [0.5, 2.0],
                                    tmp_path / "previews",
                                    ffmpeg_exe=fake)
            self.assertEqual(len(frames), 2)
            for frame in frames:
                data = Path(frame["path"]).read_bytes()
                self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
            with self.assertRaises(VideoError):
                extract_frames(video, [], tmp_path / "p2",
                               ffmpeg_exe=fake)
            with self.assertRaises(VideoError):
                extract_frames(video, [1.0] * 11, tmp_path / "p3",
                               ffmpeg_exe=fake)

    def test_extract_frames_missing_ffmpeg(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "v.mp4"
            video.write_bytes(b"fake")
            with mock.patch("hydra.video.shutil.which", return_value=None):
                with self.assertRaises(VideoUnavailableError):
                    extract_frames(video, [1.0], Path(tmp) / "p")

    def test_video_tools_shape(self):
        tools = video_tools()
        self.assertEqual(set(tools),
                         {"kdenlive", "melt", "ffmpeg", "ffprobe"})
        self.assertTrue(all(isinstance(v, bool) for v in tools.values()))


# --------------------------------------------------------------------------
# Project creation / loading / listing
# --------------------------------------------------------------------------

class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name) / "ws"
        (self.ws / "clips").mkdir(parents=True)
        (self.ws / "clips" / "a.mp4").write_bytes(b"aaa")

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_project_writes_files(self):
        result = create_project(self.ws, _valid_spec(
            assets=[{"path": "clips/a.mp4", "source": "self-made",
                     "license": "own"}]))
        self.assertEqual(result["project_id"], "demo")
        self.assertEqual(result["duration"], 5.0)
        project_dir = self.ws / "video" / "demo"
        self.assertTrue((project_dir / "timeline.json").is_file())
        self.assertTrue((project_dir / "manifest.json").is_file())
        mlt = (project_dir / "project.mlt").read_text(encoding="utf-8")
        ET.fromstring(mlt)  # valid XML
        manifest = json.loads(
            (project_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["assets"][0]["source"], "self-made")

    def test_create_duplicate_refused_without_overwrite(self):
        create_project(self.ws, _valid_spec())
        with self.assertRaises(FileExistsError):
            create_project(self.ws, _valid_spec())
        result = create_project(self.ws, _valid_spec(), overwrite=True)
        self.assertEqual(result["project_id"], "demo")

    def test_create_bad_spec_rejected(self):
        with self.assertRaises(VideoError):
            create_project(self.ws, _valid_spec(profile="nope"))
        with self.assertRaises(ValueError):
            create_project(self.ws, _valid_spec(name="../evil"))

    def test_load_and_list_projects(self):
        with self.assertRaises(FileNotFoundError):
            load_project(self.ws, "missing")
        create_project(self.ws, _valid_spec(name="one"))
        create_project(self.ws, _valid_spec(name="two"))
        projects = list_projects(self.ws)
        self.assertEqual([p["project_id"] for p in projects],
                         ["one", "two"])
        self.assertTrue(all(p["has_mlt"] for p in projects))
        loaded = load_project(self.ws, "one")
        self.assertEqual(loaded["name"], "one")
        self.assertEqual(loaded["duration"], 5.0)

    def test_list_projects_empty_without_video_dir(self):
        self.assertEqual(list_projects(self.ws), [])


# --------------------------------------------------------------------------
# Workflows (unit level, fake runners unless noted)
# --------------------------------------------------------------------------

def _fake_runners(calls: list, render_fn=None, flaky: dict | None = None):
    """Fake step runners recording (step, attempt) calls. ``flaky``
    maps step -> number of transient failures before success."""
    flaky = dict(flaky or {})
    runners = {}

    def make(step):
        def run(manager, row, ctx):
            calls.append(step)
            if step in flaky and flaky[step] > 0:
                flaky[step] -= 1
                raise TransientVideoError(f"flaky {step}")
            if step == "render" and render_fn is not None:
                return render_fn(manager, row, ctx)
            return {"fake": step}
        return run
    for step in STEPS:
        runners[step] = make(step)
    return runners


def _write_timeline_json(ws: Path, name: str, duration: float = 5.0) -> None:
    project_dir = ws / "video" / name
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "timeline.json").write_text(json.dumps({
        "name": name, "profile": "1080p30", "duration": duration,
        "width": 1920, "height": 1080, "fps_num": 30, "fps_den": 1,
        "display_aspect": [16, 9], "tracks": [], "transitions": [],
        "subtitle": None, "subtitle_abs": None,
    }), encoding="utf-8")


class WorkflowUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name) / "ws"
        self.ws.mkdir()
        self.data_dir = Path(self.tmp.name) / "data"
        self.data_dir.mkdir()
        _write_timeline_json(self.ws, "demo")
        self.managers: list[WorkflowManager] = []

    def tearDown(self):
        for manager in self.managers:
            manager.close()
        self.tmp.cleanup()

    def _manager(self, **kwargs) -> WorkflowManager:
        manager = WorkflowManager(
            self.ws, data_dir=self.data_dir, **kwargs)
        self.managers.append(manager)
        return manager

    def _wait_status(self, manager, workflow_id, statuses, timeout=15.0):
        def done():
            snap = manager.snapshot(workflow_id)
            return snap if snap and snap["status"] in statuses else None
        found = _wait_until(done, timeout=timeout)
        self.assertIsNotNone(
            found, f"workflow {workflow_id} did not reach {statuses}")
        return found

    def test_full_run_with_fake_runners(self):
        calls: list = []
        manager = self._manager(step_runners=_fake_runners(calls))
        created = manager.create_workflow("demo", "video/demo/out.mp4")
        manager.start(created["workflow_id"])
        done = self._wait_status(manager, created["workflow_id"], ("done",))
        self.assertEqual(done["progress_percent"], 100)
        self.assertEqual(calls, list(STEPS))
        self.assertTrue(all(s["status"] == "done" for s in done["steps"]))
        self.assertEqual(done["output_path"], "video/demo/out.mp4")

    def test_checkpoint_resume_skips_completed_steps(self):
        calls: list = []
        manager1 = self._manager(step_runners=_fake_runners(calls))
        created = manager1.create_workflow("demo", "video/demo/out.mp4")
        workflow_id = created["workflow_id"]
        # Simulate a crash after ingest+probe checkpointed: mark them
        # done in the DB, mark the workflow interrupted, close.
        manager1._set_step(workflow_id, "ingest", "done",
                           detail={"fake": "ingest"}, attempts=1)
        manager1._set_step(workflow_id, "probe", "done",
                           detail={"fake": "probe"}, attempts=1)
        manager1._execute(
            "UPDATE workflows SET status='interrupted', progress=15 "
            "WHERE workflow_id=?", (workflow_id,))
        manager1.close()
        self.managers.remove(manager1)

        manager2 = self._manager(step_runners=_fake_runners(calls))
        resumed = manager2.resume(workflow_id)
        self.assertEqual(resumed["status"], "running")
        done = self._wait_status(manager2, workflow_id, ("done",))
        # ingest/probe were checkpointed: never re-run.
        self.assertEqual(calls, ["timeline", "render", "validate",
                                 "preview"])
        by_step = {s["step"]: s for s in done["steps"]}
        self.assertEqual(by_step["ingest"]["attempts"], 1)
        self.assertEqual(by_step["probe"]["attempts"], 1)

    def test_recover_marks_crashed_workflow_interrupted(self):
        manager1 = self._manager()
        created = manager1.create_workflow("demo", "video/demo/out.mp4")
        workflow_id = created["workflow_id"]
        manager1._execute(
            "UPDATE workflows SET status='running', current_step='render' "
            "WHERE workflow_id=?", (workflow_id,))
        manager1._execute(
            "UPDATE workflow_steps SET status='running' "
            "WHERE workflow_id=? AND step='render'", (workflow_id,))
        manager1.close()
        self.managers.remove(manager1)

        manager2 = self._manager()  # __init__ calls recover()
        snap = manager2.snapshot(workflow_id)
        self.assertEqual(snap["status"], "interrupted")
        by_step = {s["step"]: s for s in snap["steps"]}
        self.assertEqual(by_step["render"]["status"], "pending")
        # ... and it can be resumed.
        manager2.close()
        self.managers.remove(manager2)
        calls: list = []
        manager3 = self._manager(step_runners=_fake_runners(calls))
        manager3.resume(workflow_id)
        self._wait_status(manager3, workflow_id, ("done",))

    def test_retry_with_backoff_then_success(self):
        sleeps: list = []
        calls: list = []
        manager = self._manager(
            step_runners=_fake_runners(calls, flaky={"probe": 2}),
            backoff_seconds=(2.0, 8.0),
            sleep=sleeps.append)
        created = manager.create_workflow("demo", "video/demo/out.mp4")
        manager.start(created["workflow_id"])
        done = self._wait_status(manager, created["workflow_id"], ("done",))
        by_step = {s["step"]: s for s in done["steps"]}
        self.assertEqual(by_step["probe"]["attempts"], 3)
        self.assertEqual(sleeps, [2.0, 8.0])
        self.assertEqual(calls.count("probe"), 3)

    def test_permanent_failure_no_retry(self):
        def bad_probe(manager, row, ctx):
            raise VideoError("timeline is invalid")
        runners = _fake_runners([])
        runners["probe"] = bad_probe
        sleeps: list = []
        manager = self._manager(step_runners=runners, sleep=sleeps.append)
        created = manager.create_workflow("demo", "video/demo/out.mp4")
        manager.start(created["workflow_id"])
        failed = self._wait_status(manager, created["workflow_id"],
                                   ("failed",))
        by_step = {s["step"]: s for s in failed["steps"]}
        self.assertEqual(by_step["probe"]["attempts"], 1)
        self.assertEqual(by_step["probe"]["status"], "failed")
        self.assertEqual(by_step["timeline"]["status"], "pending")
        self.assertEqual(sleeps, [])
        self.assertIn("timeline is invalid", failed["error"])

    def test_disk_guard_refuses_render(self):
        runners = _fake_runners([])
        del runners["render"]  # real render step, fake disk guard
        manager = self._manager(
            step_runners=runners,
            disk_check=lambda d, n: {"ok": False, "free_bytes": 0,
                                     "needed_bytes": n, "path": str(d)})
        created = manager.create_workflow("demo", "video/demo/out.mp4")
        manager.start(created["workflow_id"])
        failed = self._wait_status(manager, created["workflow_id"],
                                   ("failed",))
        by_step = {s["step"]: s for s in failed["steps"]}
        self.assertEqual(by_step["render"]["status"], "failed")
        self.assertIn("disk guard", by_step["render"]["error"])

    def test_resume_illegal_states(self):
        manager = self._manager(step_runners=_fake_runners([]))
        created = manager.create_workflow("demo", "video/demo/out.mp4")
        with self.assertRaises(WorkflowError):
            manager.resume(created["workflow_id"])  # pending, not failed
        with self.assertRaises(WorkflowError):
            manager.resume("no-such-workflow")
        manager.start(created["workflow_id"])
        done = self._wait_status(manager, created["workflow_id"], ("done",))
        with self.assertRaises(WorkflowError):
            manager.resume(done["workflow_id"])

    def test_create_rejects_escape_and_unknown_project(self):
        manager = self._manager()
        with self.assertRaises(ValueError):
            manager.create_workflow("demo", "../evil.mp4")
        with self.assertRaises(FileNotFoundError):
            manager.create_workflow("nope", "video/nope/out.mp4")

    def test_render_lock_exclusivity_flag(self):
        manager = self._manager()
        self.assertFalse(manager.render_busy())
        manager._render_lock.acquire()
        try:
            self.assertTrue(manager.render_busy())
        finally:
            manager._render_lock.release()
        self.assertFalse(manager.render_busy())

    @unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg/ffprobe needed")
    def test_full_real_run_with_fake_melt(self):
        """Real steps end to end; only melt is faked (it renders a
        valid mp4 with real ffmpeg). Proves ingest/probe/timeline/
        render/validate/preview integrate."""
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir()
        fake_melt = _write_exe(bin_dir / "melt", FAKE_MELT_SCRIPT)
        (bin_dir / "melt_config.json").write_text(
            json.dumps({"duration": 5}), encoding="utf-8")
        media = _make_media(self.ws / "clips" / "a.mp4", duration=5)
        self.assertTrue(media.is_file())
        # setUp already wrote a "demo" timeline fixture; use a fresh name.
        create_project(self.ws, _valid_spec(name="realrun"))
        manager = self._manager(melt_exe=fake_melt)
        created = manager.create_workflow("realrun", "video/realrun/out.mp4")
        manager.start(created["workflow_id"])
        done = self._wait_status(manager, created["workflow_id"],
                                 ("done",), timeout=60)
        self.assertEqual(done["progress_percent"], 100)
        self.assertTrue((self.ws / "video" / "realrun" / "out.mp4").is_file())
        self.assertEqual(len(done["preview_paths"]), 3)
        for rel in done["preview_paths"]:
            data = (self.ws / rel).read_bytes()
            self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))

    @unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg/ffprobe needed")
    def test_two_renders_never_overlap(self):
        """Two workflows with a slow fake melt: their melt
        invocations must not overlap (single-render queue)."""
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir()
        fake_melt = _write_exe(bin_dir / "melt", FAKE_MELT_SCRIPT)
        log_path = Path(self.tmp.name) / "melt.log"
        (bin_dir / "melt_config.json").write_text(json.dumps(
            {"duration": 5, "sleep": 0.6, "log": str(log_path)}),
            encoding="utf-8")
        _make_media(self.ws / "clips" / "a.mp4", duration=5)
        create_project(self.ws, _valid_spec(name="one"))
        create_project(self.ws, _valid_spec(name="two"))
        manager = self._manager(melt_exe=fake_melt)
        first = manager.create_workflow("one", "video/one/out.mp4")
        second = manager.create_workflow("two", "video/two/out.mp4")
        manager.start(first["workflow_id"])
        manager.start(second["workflow_id"])
        self._wait_status(manager, first["workflow_id"], ("done",),
                          timeout=60)
        self._wait_status(manager, second["workflow_id"], ("done",),
                          timeout=60)
        events = [json.loads(line) for line in
                  log_path.read_text(encoding="utf-8").splitlines()]
        starts = [e["t"] for e in events if e["event"] == "start"]
        ends = [e["t"] for e in events if e["event"] == "end"]
        self.assertEqual(len(starts), 2)
        self.assertEqual(len(ends), 2)
        intervals = sorted(zip(starts, ends))
        self.assertLessEqual(intervals[0][1], intervals[1][0])


# --------------------------------------------------------------------------
# HTTP daemon end-to-end
# --------------------------------------------------------------------------

class HttpDaemonV04Tests(unittest.TestCase):
    """End-to-end tests against a real v0.4 daemon instance."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws = root / "ws"
        self.ws.mkdir()
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
        if FFMPEG:
            _make_media(self.ws / "clips" / "a.mp4", duration=5)

    def _start_server(self):
        self.server = make_server(self.config, TEST_TOKEN)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _stop_server(self):
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

    def tearDown(self):
        self._stop_server()
        self.tmp.cleanup()

    def request(self, method, path, payload=None, token="correct"):
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

    # -- version + health --------------------------------------------------

    def test_version_is_0_4_0(self):
        self.assertEqual(VERSION, "0.4.0")
        self.assertEqual(__version__, "0.4.0")

    def test_health_reports_video_tools(self):
        status, body = self.request("GET", "/health", token="none")
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], "0.4.0")
        tools = body["video_tools"]
        self.assertEqual(set(tools),
                         {"kdenlive", "melt", "ffmpeg", "ffprobe"})
        self.assertTrue(all(isinstance(v, bool) for v in tools.values()))
        # No version guessing: booleans only.
        self.assertNotIn("melt_version", body)

    # -- /video/probe ------------------------------------------------------

    def test_video_probe_requires_auth(self):
        status, _ = self.request("POST", "/video/probe",
                                 {"path": "clips/a.mp4"}, token="none")
        self.assertEqual(status, 401)

    def test_video_probe_escape_and_missing(self):
        status, _ = self.request("POST", "/video/probe", {"path": ".."})
        self.assertEqual(status, 403)
        status, _ = self.request("POST", "/video/probe",
                                 {"path": "/etc/passwd"})
        self.assertEqual(status, 403)
        status, _ = self.request("POST", "/video/probe",
                                 {"path": "clips/nope.mp4"})
        self.assertEqual(status, 404)

    @unittest.skipUnless(FFPROBE, "ffprobe not installed")
    def test_video_probe_real_media(self):
        status, body = self.request("POST", "/video/probe",
                                    {"path": "clips/a.mp4"})
        self.assertEqual(status, 200)
        self.assertTrue(body["has_video"])
        self.assertAlmostEqual(body["duration"], 5.0, places=1)
        self.assertEqual((body["width"], body["height"]), (320, 240))

    # -- /video/project/create ----------------------------------------------

    def test_video_project_create_requires_auth(self):
        status, _ = self.request("POST", "/video/project/create",
                                 _valid_spec(), token="none")
        self.assertEqual(status, 401)

    def test_video_project_create_and_duplicate(self):
        status, body = self.request("POST", "/video/project/create",
                                    _valid_spec())
        self.assertEqual(status, 200)
        self.assertEqual(body["project_id"], "demo")
        self.assertEqual(body["duration"], 5.0)
        self.assertTrue((self.ws / "video" / "demo" / "project.mlt")
                        .is_file())
        status, _ = self.request("POST", "/video/project/create",
                                 _valid_spec())
        self.assertEqual(status, 409)
        status, body = self.request("POST", "/video/project/create",
                                    _valid_spec(overwrite=True))
        self.assertEqual(status, 200)

    def test_video_project_create_bad_spec_and_escape(self):
        status, _ = self.request("POST", "/video/project/create",
                                 _valid_spec(profile="nope"))
        self.assertEqual(status, 400)
        status, _ = self.request("POST", "/video/project/create",
                                 _valid_spec(clips=[
                                     {"asset": "../evil.mp4", "in": 0,
                                      "out": 2}]))
        self.assertEqual(status, 403)

    # -- /video/render/start + poll ------------------------------------------

    def test_video_render_start_requires_auth(self):
        status, _ = self.request("POST", "/video/render/start",
                                 {"project_id": "demo"}, token="none")
        self.assertEqual(status, 401)

    def test_video_render_start_unknown_project(self):
        status, _ = self.request("POST", "/video/render/start",
                                 {"project_id": "nope"})
        self.assertEqual(status, 404)

    def test_video_render_start_output_escape_refused(self):
        self.request("POST", "/video/project/create", _valid_spec())
        status, _ = self.request("POST", "/video/render/start",
                                 {"project_id": "demo",
                                  "output": "../evil.mp4"})
        self.assertEqual(status, 403)

    def test_video_render_start_without_melt_is_501(self):
        """With melt removed from PATH, both dry-run and real start
        return an honest 501 and create no workflow."""
        self.request("POST", "/video/project/create", _valid_spec())
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp.name)}):
            status, body = self.request(
                "POST", "/video/render/start",
                {"project_id": "demo", "dry_run": True})
            self.assertEqual(status, 501)
            self.assertIn("melt", body["error"])
            status, _ = self.request(
                "POST", "/video/render/start", {"project_id": "demo"})
            self.assertEqual(status, 501)
        status, body = self.request("POST", "/video/list", {})
        self.assertEqual(body["workflows"], [])

    def test_video_render_poll_unknown_and_auth(self):
        status, _ = self.request("POST", "/video/render/poll",
                                 {"workflow_id": "nope"}, token="none")
        self.assertEqual(status, 401)
        status, _ = self.request("POST", "/video/render/poll",
                                 {"workflow_id": "nope"})
        self.assertEqual(status, 404)

    @unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg/ffprobe needed")
    def test_full_render_end_to_end_with_fake_melt(self):
        """Fake melt on PATH + real ffmpeg/ffprobe: dry-run, start,
        poll to done, previews on disk. Proves the HTTP -> workflow
        -> melt -> validate -> preview chain (melt semantics
        themselves stay unverified until the laptop)."""
        bin_dir = Path(self.tmp.name) / "fakebin"
        bin_dir.mkdir()
        _write_exe(bin_dir / "melt", FAKE_MELT_SCRIPT)
        (bin_dir / "melt_config.json").write_text(
            json.dumps({"duration": 5}), encoding="utf-8")
        patched_path = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
        with mock.patch.dict(os.environ, {"PATH": patched_path}):
            status, body = self.request("POST", "/video/project/create",
                                        _valid_spec())
            self.assertEqual(status, 200)
            status, body = self.request(
                "POST", "/video/render/start",
                {"project_id": "demo", "dry_run": True})
            self.assertEqual(status, 200)
            self.assertTrue(body["dry_run"])
            self.assertTrue(body["melt_available"])
            self.assertGreater(body["estimated_bytes"], 0)
            status, body = self.request(
                "POST", "/video/render/start", {"project_id": "demo"})
            self.assertEqual(status, 200)
            workflow_id = body["workflow_id"]

            def done():
                _, poll = self.request(
                    "POST", "/video/render/poll",
                    {"workflow_id": workflow_id})
                return poll if poll.get("status") == "done" else None
            final = _wait_until(done, timeout=60)
            self.assertIsNotNone(final, "workflow did not finish")
            self.assertEqual(final["progress_percent"], 100)
            self.assertEqual(len(final["preview_paths"]), 3)
            self.assertTrue(
                (self.ws / "video" / "demo" / "output.mp4").is_file())

    # -- /video/preview + /video/list ----------------------------------------

    def test_video_preview_requires_auth_and_jail(self):
        status, _ = self.request("POST", "/video/preview",
                                 {"path": "clips/a.mp4"}, token="none")
        self.assertEqual(status, 401)
        status, _ = self.request("POST", "/video/preview",
                                 {"path": "../evil.mp4"})
        self.assertEqual(status, 403)
        status, _ = self.request("POST", "/video/preview",
                                 {"path": "clips/nope.mp4"})
        self.assertEqual(status, 404)

    @unittest.skipUnless(FFMPEG, "ffmpeg not installed")
    def test_video_preview_extracts_png_frames(self):
        status, body = self.request(
            "POST", "/video/preview",
            {"path": "clips/a.mp4", "timestamps": [0.5, 2.5],
             "output_dir": "previews/demo"})
        self.assertEqual(status, 200)
        self.assertEqual(len(body["paths"]), 2)
        for rel in body["paths"]:
            self.assertFalse(Path(rel).is_absolute())
            data = (self.ws / rel).read_bytes()
            self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_video_list(self):
        status, body = self.request("POST", "/video/list", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["projects"], [])
        self.assertEqual(body["workflows"], [])
        self.assertFalse(body["render_busy"])
        self.request("POST", "/video/project/create", _valid_spec())
        _, body = self.request("POST", "/video/list", {})
        self.assertEqual(len(body["projects"]), 1)
        self.assertEqual(body["projects"][0]["project_id"], "demo")

    def test_video_list_requires_auth(self):
        status, _ = self.request("POST", "/video/list", {}, token="none")
        self.assertEqual(status, 401)

    def test_video_audit_never_contains_token(self):
        self.request("POST", "/video/probe", {"path": "clips/a.mp4"})
        self.request("POST", "/video/project/create", _valid_spec())
        audit = (self.data_dir / "audit.jsonl").read_text()
        self.assertIn("/video/probe", audit)
        self.assertIn("/video/project/create", audit)
        self.assertNotIn(TEST_TOKEN, audit)


if __name__ == "__main__":
    unittest.main()
