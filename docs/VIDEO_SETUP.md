# VIDEO_SETUP.md — HYDRA v0.4 headless video (Kdenlive / MLT / melt)

## The idea, in one paragraph

Video editing on Baji's laptop is done HEADLESS. Kdenlive project
files are MLT XML: the daemon generates that XML from a timeline
spec Mikey sends, and `melt` (MLT's own renderer — the same engine
Kdenlive itself uses) renders it to an MP4 without opening any
window. No mouse, no GUI clicking, no screenshots. This matches
Baji's rule: background-only work, nothing touching the UI. The
`.mlt` file can also be opened in Kdenlive later for hand editing;
Kdenlive itself never needs to run for a render.

## Install (on the laptop, run by Baji)

```bash
sudo pacman -S kdenlive mlt ffmpeg
```

- `mlt` provides the `melt` renderer. `ffmpeg` provides `ffmpeg`
  (preview frames, subtitle burn) and `ffprobe` (media probing).
- `kdenlive` is only needed to OPEN projects by hand. Headless
  renders work without it, and `/health` reports it separately so
  the distinction stays visible.
- Verify from a terminal:

```bash
which kdenlive melt ffmpeg ffprobe
curl http://127.0.0.1:8765/health   # video_tools booleans
```

No other setup is needed. There is no Kdenlive GUI session to
start, no D-Bus service, no plugin to enable. If `melt` is
missing, render requests return an honest `501` naming the
package — never a fake success.

## How a render works (the six steps)

`POST /video/render/start` runs this fixed DAG, checkpointed in
`video.db` after every step:

1. **ingest** — verify every asset exists inside the workspace jail,
   hash it (SHA256), (re)write `manifest.json`. Nothing is ever
   downloaded: media arrives via `/write`, `/exec`, or `/jobs`
   with URLs Mikey supplies explicitly.
2. **probe** — `ffprobe` every asset; results go into the manifest.
   An unprobeable clip fails the workflow here, before melt sees it.
3. **timeline** — re-validate the timeline against fresh probes and
   rewrite `project.mlt`. The XML on disk is the checkpoint any
   resume renders from.
4. **render** — disk guard first (refuses when free space is below
   a conservative estimate), then `melt` under the single-render
   lock, then an optional ffmpeg subtitle burn (SRT/VTT sidecar).
   If the burn fails, the unburned render is kept and the failure
   is recorded as a warning.
5. **validate** — re-probe the output: video stream present,
   nonzero size, duration within max(0.5s, 2%) of the timeline.
6. **preview** — three PNG frames (10% / 50% / 90%) via ffmpeg. If
   ffmpeg is missing this step is skipped, not failed.

Poll with `POST /video/render/poll` (`progress_percent`,
`current_step`, per-step attempts/errors). Only ONE melt runs at a
time; a second workflow waits in `queued` status.

## Interruption and resume (long-horizon runs)

- A workflow interrupted by a daemon restart (crash, shutdown,
  laptop sleep) is marked `interrupted` on the next daemon start.
  Any still-living melt process from the dead daemon is killed
  (its output can no longer be trusted or tracked).
- Resume is EXPLICIT: `POST /video/render/poll` with
  `{"workflow_id": "...", "resume": true}`. Completed steps are
  never re-run; the failed/interrupted step and later steps rerun.
- Nothing auto-resumes. The daemon has no scheduler and no agent
  loop; every resume is a live request from Mikey.
- Transient render failures (melt crash/timeout) retry up to 3
  times with backoff (2s, 8s). Permanent failures (bad timeline,
  missing tool, disk guard) fail immediately.
- Render timeout: `timeout_seconds` on start, default 4 hours, max
  24 hours. A timed-out render is killed (whole process group) and
  retried as a transient failure.

## Disk space (read this before long renders)

Baji's root partition is tight (4.7 GB free was measured on
2026-11-15; re-check with `/system/info` before any big render).
The disk guard estimates output size as planning-bitrate x duration
x 2.0 safety factor + 100 MB headroom, and refuses (HTTP 507) when
free space is below that. Estimates: a 10-minute 1080p30 render is
refused below ~1.9 GB free; a 60-minute one below ~11 GB. The guard
runs twice (at start and right before melt) because free space can
change while a workflow waits in the queue. Renders write ONLY
inside the workspace (`video/<project>/...`).

## Profiles

| Profile | Size | fps | Use |
|---|---|---|---|
| `1080p30` | 1920x1080 | 30 | Long-form 16:9 |
| `1080p30-vertical` | 1080x1920 | 30 | Shorts/Reels 9:16 |
| `720p30` | 1280x720 | 30 | Drafts / small files |
| `1080p25` | 1920x1080 | 25 | 25fps sources |

Render codec is fixed: libx264 (CRF 18, medium preset) + AAC 192k.
No caller-supplied melt/ffmpeg flags exist — project data cannot
inject render options.

## What the timeline model supports (and does not)

Supported: multi-track video/audio, media trims (in/out), still
images (default 5s), pango text titles, crossfade (luma+mix) and
mix transitions between adjacent tracks, per-clip volume (linear
0–2, converted to dB for MLT's volume filter), SRT/VTT subtitle
sidecar burned by ffmpeg after melt.

NOT supported in v0.4 (deliberately small): keyframed effects,
colour grading, motion/zoom, nested timelines, Kdenlive's effects
catalogue, `.kdenlive` bin metadata. The generated file is plain
MLT XML: melt renders it, Kdenlive opens it as an MLT project, but
it is not a full `.kdenlive` project file. Crossfades need the
overlapping clips on ADJACENT tracks — v0.4 does not auto-split
one track into A/B tracks; Mikey must lay the tracks out that way.

## Live verification checklist (laptop, NOT yet done)

Everything in v0.4 was built and tested on the VM, which has
ffmpeg/ffprobe but NO melt and NO Kdenlive. The following MUST be
checked live on Baji's laptop (fresh token handoff + disk check
first) before any render is claimed to work there:

- [ ] `sudo pacman -S kdenlive mlt ffmpeg` installed; `/health`
      shows all four `video_tools` true.
- [ ] `/video/probe` on a real phone clip returns sane
      duration/resolution/fps.
- [ ] Create a 2-clip project; open `project.mlt` in Kdenlive by
      hand and confirm the timeline looks right (clip order, trims,
      gaps).
- [ ] Render a 10-second test: output exists, plays, duration
      matches, previews look right.
- [ ] Crossfade test: two overlapping clips on adjacent tracks —
      confirm the dissolve actually renders (the luma transition
      carries no resource file; this is the least certain part).
- [ ] Title test: a pango title renders with readable text (font
      present on the system).
- [ ] Volume test: a 0.5-volume clip is audibly quieter.
- [ ] Subtitle test: an SRT sidecar burns in correctly.
- [ ] Interruption test: kill the daemon mid-render, restart, poll
      shows `interrupted`, resume finishes the render.
- [ ] Disk-guard test: with little free space, start returns 507
      (do NOT actually fill the disk to test this — estimate from
      `/system/info` instead).
- [ ] Vertical test: a `1080p30-vertical` render is 1080x1920.

## Troubleshooting

- `501 melt is not installed` — install `mlt` (see above).
- `507 disk guard refused` — free space (check `/system/info`),
  then resume/start again. Never render onto a nearly-full root.
- Workflow stuck in `queued` — another render holds the lock; poll
  until it finishes. Only one render runs at a time by design.
- Workflow `failed` at `validate` with a duration mismatch — the
  melt output is shorter/longer than the timeline; check the step
  error for measured vs expected, and inspect the melt stderr tail
  in the render step detail.
- `failed` at `render` with a melt error naming a missing MLT
  module (e.g. `qimage`, `pango`) — the Arch `mlt` package build
  may lack that module; report the exact module name, do not work
  around it by hand-editing the XML on the laptop without telling
  Mikey (the XML is regenerated on every timeline step).
