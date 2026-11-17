"""HYDRA video workflows v0.4: long-horizon, resumable render jobs.

A workflow is a fixed six-step DAG for one video project::

    ingest -> probe -> timeline -> render -> validate -> preview

Every step is checkpointed in SQLite (``video.db`` in the data dir,
same WAL + row pattern as ``jobs.db`` in v0.3, but a separate file
so the two managers never contend on one connection). A workflow
interrupted by a daemon restart is marked ``interrupted`` on the
next start and can be RESUMED explicitly via
``/video/render/poll`` with ``{"resume": true}`` — completed steps
are never re-run, and nothing ever auto-resumes: the daemon still
has no scheduler and no agent loop, per Baji's constraint. Resume
is always a live request from Mikey.

* Retry: a step that raises a transient error (render crash/timeout)
  is retried up to ``max_attempts`` times with backoff between
  attempts. Permanent errors (bad timeline, missing tool, disk
  guard) fail the workflow immediately — retrying them would only
  burn time and disk.
* Single render: one module-wide render lock per WorkflowManager
  means at most one ``melt`` runs at a time. A second workflow
  waits in ``queued`` status until the lock is free. This protects
  Baji's CPU and disk from parallel renders.
* Disk guard: the render step re-checks free space right before
  rendering (space can change between start and render) and refuses
  when the estimate does not fit. The refusal is permanent for that
  attempt — resume after freeing space.
* Render recovery: the melt pid is recorded. On manager start, a
  workflow left ``running``/``queued`` by a dead daemon is marked
  ``interrupted`` and any still-living melt process is killed
  (SIGKILL to its process group), because the orphan's output can
  no longer be trusted or tracked; resume re-runs the render step
  from the already-checkpointed timeline XML. This kill is the
  ONLY case where the daemon kills a process it did not spawn in
  this process lifetime, and it only ever targets a recorded melt
  pid from its own workflow table.

NO model code, NO network calls, NO telemetry. Step runners are
plain functions; tests inject fake runners to prove checkpointing
without real renders.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .security import resolve_inside
from .video import (
    DiskSpaceError,
    VideoError,
    VideoUnavailableError,
    build_manifest,
    build_mlt_xml,
    burn_subtitles,
    check_disk_space,
    estimate_output_bytes,
    extract_frames,
    load_project,
    project_dir_for,
    probe_media,
    render_melt,
    validate_output,
    validate_timeline,
)

STEPS = ("ingest", "probe", "timeline", "render", "validate", "preview")
# Progress reported when each step COMPLETES. A running step reports
# the previous step's progress; poll also returns current_step.
STEP_PROGRESS = {"ingest": 5, "probe": 15, "timeline": 30,
                 "render": 75, "validate": 90, "preview": 100}

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = (2.0, 8.0)
DEFAULT_RENDER_TIMEOUT_SECONDS = 14400  # 4 hours; renders are slow
MAX_RENDER_TIMEOUT_SECONDS = 86400


class WorkflowError(ValueError):
    """Unknown workflow id, illegal resume/cancel, bad parameters."""


def _now() -> float:
    return time.time()


class WorkflowManager:
    """Persistent, resumable video workflows.

    ``step_runners`` (tests only) maps step names to callables
    ``fn(manager, workflow_row, ctx) -> detail dict`` replacing the
    real step implementation. ``disk_check`` replaces the disk guard
    (tests). ``melt_exe``/``ffmpeg_exe``/``ffprobe_exe`` override
    tool resolution (tests use fake binaries). ``sleep`` is the
    backoff sleeper (tests record it instead of waiting).
    """

    def __init__(self, workspace: str | Path,
                 data_dir: str | Path | None = None,
                 max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                 backoff_seconds: tuple[float, ...] = DEFAULT_BACKOFF_SECONDS,
                 step_runners: dict | None = None,
                 disk_check=None,
                 melt_exe: str | None = None,
                 ffmpeg_exe: str | None = None,
                 ffprobe_exe: str | None = None,
                 sleep=None):
        self.workspace = Path(workspace).expanduser()
        self._data_dir = Path(data_dir).expanduser() if data_dir else None
        self._max_attempts = max(1, int(max_attempts))
        self._backoff = tuple(float(b) for b in backoff_seconds)
        self._step_runners = dict(step_runners or {})
        self._disk_check = disk_check or check_disk_space
        self._melt_exe = melt_exe
        self._ffmpeg_exe = ffmpeg_exe
        self._ffprobe_exe = ffprobe_exe
        self._sleep = sleep or time.sleep
        self._render_lock = threading.Lock()
        self._db_lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self._stop = threading.Event()
        self._db: sqlite3.Connection | None = None
        self._init_db()
        self.recover()

    # -- persistence ---------------------------------------------------

    def _init_db(self) -> None:
        if self._data_dir is not None:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            db_path = str(self._data_dir / "video.db")
        else:
            db_path = ":memory:"
        self._db = sqlite3.connect(
            db_path, check_same_thread=False, timeout=5.0)
        self._db.row_factory = sqlite3.Row
        with self._db_lock:
            cur = self._db.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS workflows (
                    workflow_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    output_rel TEXT NOT NULL,
                    timeout_seconds INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    current_step TEXT,
                    progress INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    warnings_json TEXT NOT NULL DEFAULT '[]',
                    preview_paths_json TEXT NOT NULL DEFAULT '[]',
                    render_pid INTEGER,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS workflow_steps (
                    workflow_id TEXT NOT NULL,
                    step TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    started_at REAL,
                    finished_at REAL,
                    PRIMARY KEY (workflow_id, step)
                )
            """)
            self._db.commit()

    def _execute(self, sql: str, params: tuple = ()) -> None:
        with self._db_lock:
            db = self._db
            if db is None:
                return
            db.execute(sql, params)
            db.commit()

    def _fetchone(self, sql: str, params: tuple = ()):
        with self._db_lock:
            db = self._db
            if db is None:
                return None
            return db.execute(sql, params).fetchone()

    def _fetchall(self, sql: str, params: tuple = ()):
        with self._db_lock:
            db = self._db
            if db is None:
                return []
            return db.execute(sql, params).fetchall()

    def _get_row(self, workflow_id: str):
        return self._fetchone(
            "SELECT * FROM workflows WHERE workflow_id=?", (workflow_id,))

    def _steps_rows(self, workflow_id: str):
        return self._fetchall(
            "SELECT * FROM workflow_steps WHERE workflow_id=?",
            (workflow_id,))

    # -- recovery ------------------------------------------------------

    def recover(self) -> int:
        """Mark workflows left running/queued by a dead daemon as
        interrupted, reset their in-flight step to pending, and kill
        any still-living recorded melt process. Returns the number
        of workflows interrupted. Safe to call repeatedly."""
        rows = self._fetchall(
            "SELECT * FROM workflows WHERE status IN ('running','queued')")
        count = 0
        for row in rows:
            self._kill_render_pid(row["render_pid"])
            self._execute(
                """UPDATE workflows SET status='interrupted',
                       current_step=NULL, error=?,
                       render_pid=NULL, updated_at=?
                   WHERE workflow_id=?""",
                ("interrupted by daemon restart; resume explicitly",
                 _now(), row["workflow_id"]))
            self._execute(
                """UPDATE workflow_steps SET status='pending', error=NULL
                   WHERE workflow_id=? AND status='running'""",
                (row["workflow_id"],))
            count += 1
        return count

    def _kill_render_pid(self, pid: int | None) -> bool:
        """SIGKILL a recorded melt process group (best effort).
        Only ever called with a pid from our own workflow table."""
        if pid is None:
            return False
        try:
            os.killpg(pid, signal.SIGKILL)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            os.kill(pid, signal.SIGKILL)
            return True
        except OSError:
            return False

    # -- lifecycle -----------------------------------------------------

    def create_workflow(self, project_id: str, output_rel: str,
                        timeout_seconds: int | None = None) -> dict:
        """Create a workflow row (steps pending) WITHOUT starting it.
        The project must already exist (load_project raises otherwise)
        and the output path must stay inside the workspace jail."""
        timeline = load_project(self.workspace, project_id)
        resolve_inside(self.workspace, output_rel)  # raises on escape
        timeout = DEFAULT_RENDER_TIMEOUT_SECONDS if timeout_seconds is None \
            else max(60, min(int(timeout_seconds),
                             MAX_RENDER_TIMEOUT_SECONDS))
        workflow_id = uuid.uuid4().hex[:16]
        now = _now()
        self._execute(
            """INSERT INTO workflows (workflow_id, project_id, output_rel,
                   timeout_seconds, status, current_step, progress,
                   created_at, updated_at)
               VALUES (?, ?, ?, ?, 'pending', NULL, 0, ?, ?)""",
            (workflow_id, project_id, output_rel, timeout, now, now))
        for step in STEPS:
            self._execute(
                """INSERT INTO workflow_steps
                       (workflow_id, step, status) VALUES (?, ?, 'pending')""",
                (workflow_id, step))
        snapshot = self.snapshot(workflow_id)
        assert snapshot is not None
        snapshot["timeline_duration"] = timeline.get("duration")
        return snapshot

    def start(self, workflow_id: str) -> dict:
        """Start (or restart) the runner thread for a pending
        workflow. Raises WorkflowError for unknown ids and for
        workflows already running/queued/done."""
        row = self._get_row(workflow_id)
        if row is None:
            raise WorkflowError(f"unknown workflow_id: {workflow_id!r}")
        if row["status"] in ("running", "queued"):
            raise WorkflowError(
                f"workflow {workflow_id} is already {row['status']}")
        if row["status"] == "done":
            raise WorkflowError(f"workflow {workflow_id} is already done")
        self._execute(
            "UPDATE workflows SET status='running', started_at=?, "
            "updated_at=?, error=NULL WHERE workflow_id=?",
            (_now(), _now(), workflow_id))
        thread = threading.Thread(
            target=self._run, args=(workflow_id,),
            name=f"hydra-workflow-{workflow_id}", daemon=True)
        self._threads[workflow_id] = thread
        thread.start()
        snapshot = self.snapshot(workflow_id)
        assert snapshot is not None
        return snapshot

    def resume(self, workflow_id: str) -> dict:
        """Resume an interrupted/failed workflow from its last
        completed step. Completed steps are kept; the failed step and
        everything after it return to pending. Raises WorkflowError
        for running/done/cancelled workflows."""
        row = self._get_row(workflow_id)
        if row is None:
            raise WorkflowError(f"unknown workflow_id: {workflow_id!r}")
        if row["status"] not in ("interrupted", "failed"):
            raise WorkflowError(
                f"workflow {workflow_id} cannot be resumed from status "
                f"{row['status']!r}")
        # Reset the failed step and all later steps to pending, keep
        # completed steps (and their attempt counts) as checkpoints.
        steps = {r["step"]: r["status"] for r in self._steps_rows(workflow_id)}
        failed_at = next(
            (s for s in STEPS if steps.get(s) in ("failed", "running")),
            STEPS[-1])
        reset_from = STEPS.index(failed_at)
        for step in STEPS[reset_from:]:
            self._execute(
                """UPDATE workflow_steps SET status='pending', error=NULL
                   WHERE workflow_id=? AND step=?""",
                (workflow_id, step))
        self._execute(
            "UPDATE workflows SET status='running', error=NULL, "
            "render_pid=NULL, updated_at=? WHERE workflow_id=?",
            (_now(), workflow_id))
        thread = threading.Thread(
            target=self._run, args=(workflow_id,),
            name=f"hydra-workflow-{workflow_id}-resume", daemon=True)
        self._threads[workflow_id] = thread
        thread.start()
        snapshot = self.snapshot(workflow_id)
        assert snapshot is not None
        return snapshot

    def cancel(self, workflow_id: str) -> dict:
        """Cancel a workflow: kill its render process (if any) and
        mark it cancelled. Terminal states are left alone."""
        row = self._get_row(workflow_id)
        if row is None:
            raise WorkflowError(f"unknown workflow_id: {workflow_id!r}")
        if row["status"] in ("done", "failed", "cancelled"):
            snapshot = self.snapshot(workflow_id)
            assert snapshot is not None
            return snapshot
        self._kill_render_pid(row["render_pid"])
        self._execute(
            """UPDATE workflows SET status='cancelled', finished_at=?,
                   updated_at=?, render_pid=NULL WHERE workflow_id=?""",
            (_now(), _now(), workflow_id))
        snapshot = self.snapshot(workflow_id)
        assert snapshot is not None
        return snapshot

    def interrupt_all(self) -> int:
        """Shutdown path: kill render processes and mark running/
        queued workflows interrupted (resumable), like recover()
        but for a live manager. Returns the count interrupted."""
        rows = self._fetchall(
            "SELECT * FROM workflows WHERE status IN ('running','queued')")
        for row in rows:
            self._kill_render_pid(row["render_pid"])
            self._execute(
                """UPDATE workflows SET status='interrupted',
                       current_step=NULL,
                       error='interrupted by daemon shutdown; resume explicitly',
                       render_pid=NULL, updated_at=?
                   WHERE workflow_id=?""",
                (_now(), row["workflow_id"]))
            self._execute(
                """UPDATE workflow_steps SET status='pending', error=NULL
                   WHERE workflow_id=? AND status='running'""",
                (row["workflow_id"],))
        return len(rows)

    def close(self) -> None:
        """Stop accepting new runner work and close the database.
        Running runner threads are daemon threads; callers that need
        a clean stop should call interrupt_all() first (the daemon
        does this on /shutdown)."""
        self._stop.set()
        with self._db_lock:
            db, self._db = self._db, None
        if db is not None:
            try:
                db.close()
            except sqlite3.Error:
                pass

    # -- reads -----------------------------------------------------------

    def snapshot(self, workflow_id: str) -> dict | None:
        row = self._get_row(workflow_id)
        if row is None:
            return None
        steps = [{
            "step": r["step"], "status": r["status"],
            "attempts": r["attempts"], "error": r["error"],
            "detail": json.loads(r["detail_json"] or "{}"),
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
        } for r in self._steps_rows(workflow_id)]
        by_name = {s["step"]: s for s in steps}
        ordered = [by_name[s] for s in STEPS if s in by_name]
        return {
            "workflow_id": row["workflow_id"],
            "project_id": row["project_id"],
            "output_path": row["output_rel"],
            "status": row["status"],
            "current_step": row["current_step"],
            "progress_percent": row["progress"],
            "error": row["error"],
            "warnings": json.loads(row["warnings_json"] or "[]"),
            "preview_paths": json.loads(row["preview_paths_json"] or "[]"),
            "timeout_seconds": row["timeout_seconds"],
            "steps": ordered,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        }

    def list_workflows(self) -> list[dict]:
        rows = self._fetchall(
            "SELECT * FROM workflows ORDER BY created_at ASC")
        summaries = []
        for row in rows:
            summaries.append({
                "workflow_id": row["workflow_id"],
                "project_id": row["project_id"],
                "output_path": row["output_rel"],
                "status": row["status"],
                "current_step": row["current_step"],
                "progress_percent": row["progress"],
                "error": row["error"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "finished_at": row["finished_at"],
            })
        return summaries

    def render_busy(self) -> bool:
        """True while any workflow holds the render lock."""
        locked = self._render_lock.acquire(blocking=False)
        if locked:
            self._render_lock.release()
            return False
        return True

    # -- runner ----------------------------------------------------------

    def _set_step(self, workflow_id: str, step: str, status: str,
                  error: str | None = None,
                  detail: dict | None = None,
                  attempts: int | None = None) -> None:
        if status == "running":
            self._execute(
                """UPDATE workflow_steps SET status='running',
                       started_at=?, error=NULL WHERE workflow_id=? AND step=?""",
                (_now(), workflow_id, step))
        elif status in ("done", "failed", "skipped"):
            self._execute(
                """UPDATE workflow_steps SET status=?, error=?,
                       detail_json=?, finished_at=? WHERE workflow_id=? AND step=?""",
                (status, error, json.dumps(detail or {}), _now(),
                 workflow_id, step))
        else:
            self._execute(
                "UPDATE workflow_steps SET status=? WHERE workflow_id=? "
                "AND step=?", (status, workflow_id, step))
        if attempts is not None:
            self._execute(
                "UPDATE workflow_steps SET attempts=? "
                "WHERE workflow_id=? AND step=?",
                (attempts, workflow_id, step))

    def _set_workflow(self, workflow_id: str, **fields) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        params = tuple(fields.values()) + (_now(), workflow_id)
        self._execute(
            f"UPDATE workflows SET {sets}, updated_at=? "
            "WHERE workflow_id=?", params)

    def _run(self, workflow_id: str) -> None:
        """Runner thread: execute pending steps in order, checkpoint
        each one, retry transient failures with backoff. Never raises
        out of the thread: unexpected exceptions fail the workflow
        with their message recorded."""
        try:
            self._run_steps(workflow_id)
        except Exception as exc:  # never die silently
            self._execute(
                """UPDATE workflows SET status='failed', error=?,
                       finished_at=?, updated_at=? WHERE workflow_id=?""",
                (f"workflow runner error: {exc}", _now(), _now(),
                 workflow_id))

    def _run_steps(self, workflow_id: str) -> None:
        row = self._get_row(workflow_id)
        if row is None:
            return
        ctx: dict = {"workflow_id": workflow_id,
                     "project_id": row["project_id"],
                     "output_rel": row["output_rel"],
                     "timeout_seconds": row["timeout_seconds"],
                     "warnings": []}
        for step in STEPS:
            if self._stop.is_set():
                return
            live = self._get_row(workflow_id)
            if live is None or live["status"] not in ("running", "queued"):
                # Cancelled or interrupted (shutdown/recovery) while a
                # step was running: stop without overwriting the status
                # the canceller set.
                return
            step_row = self._fetchone(
                "SELECT * FROM workflow_steps WHERE workflow_id=? "
                "AND step=?", (workflow_id, step))
            if step_row is None:
                continue
            if step_row["status"] in ("done", "skipped"):
                ctx[f"{step}_detail"] = json.loads(
                    step_row["detail_json"] or "{}")
                continue
            self._set_workflow(workflow_id, current_step=step)
            detail = self._run_step_with_retries(workflow_id, step, ctx)
            if detail is None:
                return  # step failed terminally; status already set
            ctx[f"{step}_detail"] = detail
            self._set_workflow(
                workflow_id, progress=STEP_PROGRESS[step])
        self._set_workflow(
            workflow_id, status="done", current_step=None,
            progress=100, finished_at=_now(),
            warnings_json=json.dumps(ctx["warnings"]))
        # Preview paths land in ctx from the preview step.
        if ctx.get("preview_paths"):
            self._set_workflow(
                workflow_id,
                preview_paths_json=json.dumps(ctx["preview_paths"]))

    def _run_step_with_retries(self, workflow_id: str, step: str,
                               ctx: dict) -> dict | None:
        """Run one step with retries. Returns the step detail on
        success (or skip), None when the workflow has failed."""
        runner = self._step_runners.get(step) or getattr(
            self, f"_step_{step}")
        attempts = 0
        while attempts < self._max_attempts:
            attempts += 1
            self._set_step(workflow_id, step, "running",
                           attempts=attempts)
            try:
                detail = runner(self, _row_dict(self._get_row(workflow_id)),
                                ctx)
            except Exception as exc:
                transient = getattr(exc, "transient", False)
                live = self._get_row(workflow_id)
                if live is not None and live["status"] not in (
                        "running", "queued"):
                    # Cancelled/interrupted mid-step (e.g. the render
                    # process was killed by cancel()): do not retry
                    # and do not overwrite the terminal status.
                    return None
                if transient and attempts < self._max_attempts \
                        and not self._stop.is_set():
                    delay = self._backoff[min(
                        attempts - 1, len(self._backoff) - 1)] \
                        if self._backoff else 0.0
                    self._set_step(workflow_id, step, "pending",
                                   error=str(exc), attempts=attempts)
                    if delay > 0:
                        self._sleep(delay)
                    continue
                self._set_step(workflow_id, step, "failed",
                               error=str(exc), attempts=attempts)
                self._set_workflow(workflow_id, status="failed",
                                   current_step=None, error=str(exc),
                                   finished_at=_now())
                return None
            if isinstance(detail, dict) and detail.get("_skipped"):
                self._set_step(workflow_id, step, "skipped",
                               detail=detail, attempts=attempts)
            else:
                self._set_step(workflow_id, step, "done",
                               detail=detail or {}, attempts=attempts)
            return detail or {}
        return None

    # -- real step implementations ---------------------------------------

    def _step_ingest(self, manager, row: dict, ctx: dict) -> dict:
        """Verify every asset exists inside the jail, hash it, and
        (re)write the project manifest. Never downloads anything."""
        project_dir = project_dir_for(self.workspace, row["project_id"])
        timeline = load_project(self.workspace, row["project_id"])
        entries: dict[str, dict] = {}
        for track in timeline.get("tracks", []):
            for clip in track.get("clips", []):
                if clip.get("asset"):
                    entries.setdefault(clip["asset"],
                                       {"path": clip["asset"]})
        manifest_path = project_dir / "manifest.json"
        if manifest_path.is_file():
            try:
                old = json.loads(manifest_path.read_text(encoding="utf-8"))
                for asset in old.get("assets", []):
                    if asset.get("path") in entries:
                        entries[asset["path"]] = {
                            "path": asset["path"],
                            "source": asset.get("source", ""),
                            "source_url": asset.get("source_url", ""),
                            "license": asset.get("license", ""),
                        }
            except (OSError, json.JSONDecodeError):
                pass  # stale manifest: rebuild from scratch below
        manifest = build_manifest(
            self.workspace, list(entries.values()),
            ffprobe_exe=self._ffprobe_exe, probe_if_available=False)
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
        ctx["manifest"] = manifest
        return {"manifest_path": f"video/{row['project_id']}/manifest.json",
                "asset_count": len(manifest["assets"])}

    def _step_probe(self, manager, row: dict, ctx: dict) -> dict:
        """ffprobe every asset; store results in the manifest. A
        media asset that cannot be probed fails the workflow: an
        unprobed clip must never reach melt silently."""
        project_dir = project_dir_for(self.workspace, row["project_id"])
        manifest_path = project_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        probes: dict[str, dict] = {}
        for asset in manifest["assets"]:
            resolved = resolve_inside(self.workspace, asset["path"])
            probe = probe_media(resolved, ffprobe_exe=self._ffprobe_exe)
            probe.pop("path", None)
            asset["probe"] = probe
            asset["probe_error"] = None
            probes[asset["path"]] = probe
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
        ctx["probes"] = probes
        return {"probed_assets": len(probes)}

    def _step_timeline(self, manager, row: dict, ctx: dict) -> dict:
        """Re-validate the timeline against fresh probes and rewrite
        the MLT XML. The XML on disk is the checkpoint the render
        step (and any resume) renders from."""
        project_dir = project_dir_for(self.workspace, row["project_id"])
        timeline_path = project_dir / "timeline.json"
        spec = json.loads(timeline_path.read_text(encoding="utf-8"))
        probes = ctx.get("probes")
        if probes is None:
            manifest = json.loads(
                (project_dir / "manifest.json").read_text(
                    encoding="utf-8"))
            probes = {a["path"]: a.get("probe")
                      for a in manifest["assets"]}
        timeline = validate_timeline(spec, self.workspace, probes=probes)
        xml_text = build_mlt_xml(timeline)
        mlt_path = project_dir / "project.mlt"
        mlt_path.write_text(xml_text, encoding="utf-8")
        ctx["timeline"] = timeline
        return {"mlt_path": f"video/{row['project_id']}/project.mlt",
                "duration": timeline["duration"],
                "profile": timeline["profile"]}

    def _step_render(self, manager, row: dict, ctx: dict) -> dict:
        """Disk guard, then melt (single-render lock held), then an
        optional subtitle burn. The burn failure path keeps the melt
        output and records a warning instead of failing the render.
        """
        import shutil as _shutil

        project_dir = project_dir_for(self.workspace, row["project_id"])
        timeline = ctx.get("timeline") or load_project(
            self.workspace, row["project_id"])
        duration = float(timeline["duration"])
        profile = str(timeline["profile"])
        needed = estimate_output_bytes(duration, profile)
        guard = self._disk_check(self.workspace, needed)
        if not guard["ok"]:
            raise DiskSpaceError(
                "disk guard refused the render: "
                f"free {guard['free_bytes']} bytes < estimated need "
                f"{needed} bytes for a {duration:.1f}s {profile} render; "
                "free space and resume")
        if self._melt_exe is None and _shutil.which("melt") is None:
            raise VideoUnavailableError(
                "melt is not installed; install it with: "
                "sudo pacman -S mlt (see docs/VIDEO_SETUP.md)")
        output_rel = row["output_rel"]
        output_path = resolve_inside(self.workspace, output_rel)
        mlt_path = project_dir / "project.mlt"
        if not mlt_path.is_file():
            raise VideoError("timeline XML missing; timeline step "
                             "did not produce project.mlt")

        # Single render at a time: wait in "queued" until free.
        self._set_workflow(row["workflow_id"], status="queued")
        with self._render_lock:
            self._set_workflow(row["workflow_id"], status="running")
            melt_target = output_path
            raw_path: Path | None = None
            if timeline.get("subtitle"):
                raw_path = output_path.with_name(
                    output_path.stem + ".raw" + output_path.suffix)
                melt_target = raw_path

            def _record_pid(pid: int) -> None:
                self._set_workflow(row["workflow_id"], render_pid=pid)

            result = render_melt(
                mlt_path, melt_target,
                timeout_seconds=int(row["timeout_seconds"]),
                melt_exe=self._melt_exe, on_pid=_record_pid)
            self._set_workflow(row["workflow_id"], render_pid=None)
        detail = {"output_path": output_rel,
                  "size_bytes": result["size_bytes"],
                  "subtitles_burned": False,
                  "stderr_tail": result["stderr_tail"][-500:]}
        if timeline.get("subtitle") and raw_path is not None:
            subtitle_path = resolve_inside(
                self.workspace, str(timeline["subtitle"]))
            try:
                burned = burn_subtitles(
                    raw_path, subtitle_path, output_path,
                    ffmpeg_exe=self._ffmpeg_exe)
                detail["size_bytes"] = burned["size_bytes"]
                detail["subtitles_burned"] = True
                try:
                    raw_path.unlink(missing_ok=True)
                except OSError:
                    pass
            except (VideoError, VideoUnavailableError) as exc:
                # Keep the good melt render; report the burn failure
                # as a warning, not a workflow failure.
                warning = f"subtitle burn failed ({exc}); kept unburned render"
                ctx["warnings"].append(warning)
                detail["subtitle_warning"] = warning
                try:
                    raw_path.replace(output_path)
                except OSError:
                    pass
        ctx["render_detail"] = detail
        return detail

    def _step_validate(self, manager, row: dict, ctx: dict) -> dict:
        """Re-probe the render and check duration/streams."""
        timeline = ctx.get("timeline") or load_project(
            self.workspace, row["project_id"])
        output_path = resolve_inside(self.workspace, row["output_rel"])
        result = validate_output(
            output_path, float(timeline["duration"]),
            ffprobe_exe=self._ffprobe_exe)
        ctx["validation"] = result
        return {"duration": result["duration"],
                "expected_duration": result["expected_duration"],
                "width": result["width"], "height": result["height"],
                "size_bytes": result["size_bytes"]}

    def _step_preview(self, manager, row: dict, ctx: dict) -> dict:
        """Extract 3 preview frames (10/50/90%). When ffmpeg is
        missing the step is SKIPPED (not failed): the render itself
        is done and valid, only the thumbnails are absent."""
        import shutil as _shutil

        if self._ffmpeg_exe is None and _shutil.which("ffmpeg") is None:
            return {"_skipped": True,
                    "reason": "ffmpeg is not installed; previews skipped"}
        timeline = ctx.get("timeline") or load_project(
            self.workspace, row["project_id"])
        duration = float(timeline["duration"])
        timestamps = [round(duration * f, 3) for f in (0.1, 0.5, 0.9)]
        output_path = resolve_inside(self.workspace, row["output_rel"])
        preview_dir = project_dir_for(
            self.workspace, row["project_id"]) / "previews"
        frames = extract_frames(
            output_path, timestamps, preview_dir,
            ffmpeg_exe=self._ffmpeg_exe)
        rel_paths = []
        for frame in frames:
            rel = str(Path(frame["path"]).relative_to(self.workspace))
            rel_paths.append(rel)
        ctx["preview_paths"] = rel_paths
        return {"preview_paths": rel_paths, "timestamps": timestamps}


def _row_dict(row) -> dict:
    if row is None:
        return {}
    try:
        return dict(row)
    except (TypeError, ValueError):
        return {}
