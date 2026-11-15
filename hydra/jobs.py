"""HYDRA background jobs v0.3: persistent + detached jobs.

A job is a shell command started by the daemon (through the SAME
blocklist and jail checks as the synchronous /exec endpoint) whose
output the caller retrieves later with /jobs/poll.

v0.3 persistence (new in this release):

* When the JobManager is given a ``data_dir``, every job is recorded
  in a SQLite database (``jobs.db``) and its stdout/stderr go to
  per-job log files (``job-logs/<job_id>.out`` / ``.err``) under the
  data dir. Job records, output, and (for detached jobs) the running
  processes survive a daemon restart: a new daemon re-attaches to
  still-running jobs by PID liveness (``os.kill(pid, 0)``), guarded
  against PID reuse by comparing the process start time from
  ``/proc/<pid>/stat`` when available.
* ``detached`` jobs (``"detached": true`` on /jobs/start) run in
  their own session with output redirected straight to their log
  files, and are NOT killed when the daemon shuts down. They keep
  running across daemon restarts and are re-attached on the next
  start. ``/jobs/kill`` kills a detached job's whole process group.
* Non-detached jobs are still killed on clean daemon shutdown
  (``/shutdown`` / Ctrl-C / atexit), exactly as in v0.2 — but their
  records and captured output remain queryable after a restart.
  If the daemon dies unexpectedly (crash / SIGKILL), non-detached
  jobs keep running as orphans and are re-attached the same way;
  their output stops at the moment the daemon died only if they
  were writing to pipes — in persistent mode output goes to files,
  so it keeps flowing.

Safety notes, stated plainly:

* Output is capped at read time (200KB per stream, same as /exec);
  the ``truncated`` flag says when a log file holds more. Log files
  themselves are hard-capped at 1MB per stream via RLIMIT_FSIZE so a
  runaway job cannot fill the disk (Baji's root partition is tight):
  a job that hits the cap is killed by the kernel (SIGXFSZ). The
  limit is set with a single ``setrlimit`` syscall in the child
  between fork and exec — no locks, no allocations.
* Deadlines are wall-clock (epoch seconds) so they survive restarts.
  The reaper enforces them once a second, in the daemon that happens
  to be running.
* Re-attached jobs have no exit code: the daemon cannot ``wait()``
  a process it did not spawn, so a re-attached job that is found
  dead is reported ``done`` with ``returncode: null`` and
  ``recovered: true``. Nothing is faked: ``null`` means unknown.
* Without a ``data_dir`` (only used by direct unit-test
  construction), jobs behave exactly as in v0.2: in-memory only,
  pipes + reader threads, forgotten on manager shutdown.

NO model code, NO network calls, NO telemetry. Output caps,
32-job cap, and whole-process-group kills are unchanged from v0.2.
"""

from __future__ import annotations

import os
import resource
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

MAX_JOBS = 32
MAX_OUTPUT_BYTES = 200 * 1024  # per stream, same as synchronous /exec
MAX_LOG_BYTES = 1024 * 1024  # RLIMIT_FSIZE per stream log file (1MB)
REAPER_INTERVAL_SECONDS = 1.0


def _proc_starttime(pid: int) -> str | None:
    """Process start time (clock ticks since boot) from /proc/<pid>/stat.

    Used to guard PID-liveness checks against PID reuse. Returns None
    when /proc is unavailable or the process is gone.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(
            encoding="ascii", errors="replace")
    except OSError:
        return None
    # comm (field 2) is parenthesised and may contain spaces/parens.
    rparen = stat.rfind(")")
    if rparen < 0:
        return None
    fields = stat[rparen + 2:].split()
    # fields[0] is field 3 (state); starttime is field 22 -> index 19.
    if len(fields) < 20:
        return None
    return fields[19]


def _pid_alive(pid: int, starttime: str | None) -> bool:
    """True when ``pid`` exists AND (when known) is the same process
    that was started (starttime match guards against PID reuse)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    if starttime is not None:
        current = _proc_starttime(pid)
        if current is not None and current != starttime:
            return False
    return True


def _limit_child_files() -> None:
    """preexec_fn: cap each output file at MAX_LOG_BYTES.

    A single setrlimit syscall only — safe between fork and exec.
    Exceeding the cap delivers SIGXFSZ to the child (default action:
    terminate), so a runaway job cannot fill the disk.
    """
    resource.setrlimit(
        resource.RLIMIT_FSIZE, (MAX_LOG_BYTES, MAX_LOG_BYTES))


@dataclass
class Job:
    job_id: str
    command: str
    cwd: Path
    proc: subprocess.Popen | None
    deadline_epoch: float | None  # wall-clock epoch, or None for no timeout
    sandboxed: bool = False
    detached: bool = False
    status: str = "running"  # running | done | killed
    returncode: int | None = None
    timed_out: bool = False
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    pid: int | None = None
    pgid: int | None = None
    proc_starttime: str | None = None
    recovered: bool = False  # re-attached after a daemon restart
    stdout_path: Path | None = None  # persistent mode: log files
    stderr_path: Path | None = None
    _stdout_chunks: list[str] = field(default_factory=list, repr=False)
    _stderr_chunks: list[str] = field(default_factory=list, repr=False)
    _stdout_bytes: int = 0
    _stderr_bytes: int = 0
    _truncated_out: bool = False
    _truncated_err: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- output capture (in-memory mode only) -------------------------

    def _append(self, stream: str, text: str) -> None:
        data = text.encode("utf-8", errors="replace")
        with self._lock:
            if stream == "stdout":
                room = MAX_OUTPUT_BYTES - self._stdout_bytes
                if room <= 0:
                    self._truncated_out = True
                    return
                keep = data[:room].decode("utf-8", errors="replace")
                self._stdout_chunks.append(keep)
                self._stdout_bytes += len(keep.encode("utf-8"))
                if len(data) > room:
                    self._truncated_out = True
            else:
                room = MAX_OUTPUT_BYTES - self._stderr_bytes
                if room <= 0:
                    self._truncated_err = True
                    return
                keep = data[:room].decode("utf-8", errors="replace")
                self._stderr_chunks.append(keep)
                self._stderr_bytes += len(keep.encode("utf-8"))
                if len(data) > room:
                    self._truncated_err = True

    # -- reads ----------------------------------------------------------

    def _read_capped(self, path: Path | None) -> tuple[str, bool]:
        """First MAX_OUTPUT_BYTES of a log file + truncated flag."""
        if path is None:
            return "", False
        try:
            with open(path, "rb") as fh:
                data = fh.read(MAX_OUTPUT_BYTES + 1)
        except OSError:
            return "", False
        truncated = len(data) > MAX_OUTPUT_BYTES
        return (data[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"),
                truncated)

    def snapshot(self, stdout_offset: int = 0) -> dict:
        """Current state + captured output (stdout from ``offset``).

        Same response shape as v0.2, plus the additive ``detached``,
        ``pid`` and ``recovered`` fields. In persistent mode the
        output is read live from the log files; in-memory mode uses
        the captured chunks, exactly as before.
        """
        with self._lock:
            status = self.status
            returncode = self.returncode
            timed_out = self.timed_out
            finished_at = self.finished_at
            stdout_path = self.stdout_path
            stderr_path = self.stderr_path
            if stdout_path is None:
                stdout = "".join(self._stdout_chunks)
                stderr = "".join(self._stderr_chunks)
                truncated = self._truncated_out or self._truncated_err
            else:
                stdout = stderr = ""
                truncated = False
        if stdout_path is not None:
            stdout, trunc_out = self._read_capped(stdout_path)
            stderr, trunc_err = self._read_capped(stderr_path)
            truncated = trunc_out or trunc_err
        total_len = len(stdout)
        offset = max(0, min(stdout_offset, total_len))
        return {
            "job_id": self.job_id,
            "status": status,
            "returncode": returncode,
            "timed_out": timed_out,
            "stdout": stdout[offset:],
            "stdout_total_len": total_len,
            "stdout_offset": offset,
            "stderr": stderr,
            "truncated": truncated,
            "sandboxed": self.sandboxed,
            "detached": self.detached,
            "pid": self.pid,
            "recovered": self.recovered,
            "started_at": self.started_at,
            "finished_at": finished_at,
        }

    def summary(self) -> dict:
        with self._lock:
            status = self.status
            returncode = self.returncode
        return {
            "job_id": self.job_id,
            "command_preview": self.command[:200],
            "status": status,
            "returncode": returncode,
            "timed_out": self.timed_out,
            "sandboxed": self.sandboxed,
            "detached": self.detached,
            "pid": self.pid,
            "recovered": self.recovered,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class JobManager:
    """Thread-safe registry of background jobs, optionally persistent.

    With ``data_dir`` given, job records live in ``jobs.db`` and
    output in ``job-logs/`` under that directory, and running jobs
    are re-attached after a restart. Without it, jobs are purely
    in-memory (v0.2 behaviour).
    """

    def __init__(self, max_jobs: int = MAX_JOBS,
                 data_dir: str | Path | None = None):
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []  # insertion order, for pruning
        self._lock = threading.Lock()
        self._max_jobs = max_jobs
        self._data_dir = Path(data_dir).expanduser() if data_dir else None
        self._db: sqlite3.Connection | None = None
        self._db_lock = threading.Lock()
        self._log_dir: Path | None = None
        if self._data_dir is not None:
            try:
                self._init_persistence()
            except Exception as exc:  # degrade to in-memory, loudly
                import sys
                print(f"[hydra] jobs persistence unavailable "
                      f"({exc}); running in-memory only", file=sys.stderr)
                self._data_dir = None
                self._db = None
                self._log_dir = None
        self._reaper_stop = threading.Event()
        self._reaper = threading.Thread(
            target=self._reap_loop, name="hydra-job-reaper", daemon=True)
        self._reaper.start()

    # -- persistence ---------------------------------------------------

    def _init_persistence(self) -> None:
        assert self._data_dir is not None
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._log_dir = self._data_dir / "job-logs"
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(
            str(self._data_dir / "jobs.db"),
            check_same_thread=False, timeout=5.0)
        self._db.row_factory = sqlite3.Row
        with self._db_lock:
            cur = self._db.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    command TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    pid INTEGER,
                    pgid INTEGER,
                    proc_starttime TEXT,
                    status TEXT NOT NULL,
                    returncode INTEGER,
                    timed_out INTEGER NOT NULL DEFAULT 0,
                    sandboxed INTEGER NOT NULL DEFAULT 0,
                    detached INTEGER NOT NULL DEFAULT 0,
                    started_at REAL NOT NULL,
                    finished_at REAL,
                    deadline_epoch REAL
                )
            """)
            self._db.commit()
        self._load_existing()

    def _db_execute(self, sql: str, params: tuple = ()) -> None:
        # Capture the connection under the lock: close() nulls it
        # under the same lock, so a waiter thread can never use a
        # connection that another thread just closed.
        with self._db_lock:
            db = self._db
            if db is None:
                return
            try:
                db.execute(sql, params)
                db.commit()
            except sqlite3.Error as exc:
                import sys
                print(f"[hydra] jobs.db write failed ({exc}); "
                      "continuing in-memory", file=sys.stderr)

    def _persist_insert(self, job: Job) -> None:
        self._db_execute(
            """INSERT INTO jobs (job_id, command, cwd, pid, pgid,
                   proc_starttime, status, returncode, timed_out,
                   sandboxed, detached, started_at, finished_at,
                   deadline_epoch)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (job.job_id, job.command, str(job.cwd), job.pid, job.pgid,
             job.proc_starttime, job.status, job.returncode,
             1 if job.timed_out else 0, 1 if job.sandboxed else 0,
             1 if job.detached else 0, job.started_at, job.finished_at,
             job.deadline_epoch))

    def _persist_update(self, job: Job) -> None:
        with job._lock:
            status = job.status
            returncode = job.returncode
            timed_out = job.timed_out
            finished_at = job.finished_at
        self._db_execute(
            """UPDATE jobs SET status=?, returncode=?, timed_out=?,
                   finished_at=? WHERE job_id=?""",
            (status, returncode, 1 if timed_out else 0, finished_at,
             job.job_id))

    def _persist_delete(self, job_id: str) -> None:
        self._db_execute("DELETE FROM jobs WHERE job_id=?", (job_id,))

    def _load_existing(self) -> None:
        """Re-attach to jobs recorded by a previous daemon process."""
        assert self._db is not None and self._log_dir is not None
        with self._db_lock:
            rows = self._db.execute(
                "SELECT * FROM jobs ORDER BY started_at ASC").fetchall()
        loaded: list[Job] = []
        for row in rows:
            job = Job(
                job_id=row["job_id"],
                command=row["command"],
                cwd=Path(row["cwd"]),
                proc=None,
                deadline_epoch=row["deadline_epoch"],
                sandboxed=bool(row["sandboxed"]),
                detached=bool(row["detached"]),
                status=row["status"],
                returncode=row["returncode"],
                timed_out=bool(row["timed_out"]),
                started_at=row["started_at"],
                finished_at=row["finished_at"],
                pid=row["pid"],
                pgid=row["pgid"],
                proc_starttime=row["proc_starttime"],
                recovered=True,
                stdout_path=self._log_dir / f"{row['job_id']}.out",
                stderr_path=self._log_dir / f"{row['job_id']}.err",
            )
            if job.status == "running":
                if job.pid is not None and _pid_alive(
                        job.pid, job.proc_starttime):
                    pass  # still running: re-attached
                else:
                    # Gone while the daemon was down. Exit code unknown.
                    job.status = "done"
                    job.returncode = None
                    job.finished_at = time.time()
                    self._persist_update(job)
            loaded.append(job)
        with self._lock:
            for job in loaded:
                self._jobs[job.job_id] = job
                self._order.append(job.job_id)
            self._prune_locked()

    # -- lifecycle -----------------------------------------------------

    def start(
        self,
        argv: list[str],
        command: str,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: int | None,
        sandboxed: bool = False,
        detached: bool = False,
    ) -> Job:
        """Start a job. ``argv`` is already built by the caller (the
        daemon), after blocklist + jail checks. Raises RuntimeError
        when the job table is full of running jobs.

        ``detached`` jobs survive daemon shutdown (they are skipped
        by kill_all) and are re-attached on restart. In persistent
        mode their output — like every persistent job's — goes
        straight to log files.
        """
        with self._lock:
            self._prune_locked()
            running = sum(
                1 for j in self._jobs.values() if j.status == "running")
            if running >= self._max_jobs:
                raise RuntimeError(
                    f"too many running jobs ({self._max_jobs}); "
                    "wait for one to finish or kill it first")
        job_id = uuid.uuid4().hex[:16]
        deadline_epoch = (
            time.time() + timeout_seconds
            if timeout_seconds and timeout_seconds > 0 else None
        )
        persistent = self._db is not None and self._log_dir is not None
        if persistent:
            assert self._log_dir is not None
            stdout_path = self._log_dir / f"{job_id}.out"
            stderr_path = self._log_dir / f"{job_id}.err"
            out_fh = open(stdout_path, "ab")
            err_fh = open(stderr_path, "ab")
            try:
                proc = subprocess.Popen(
                    argv,
                    cwd=str(cwd),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=out_fh,
                    stderr=err_fh,
                    start_new_session=True,
                    preexec_fn=_limit_child_files,
                )
            finally:
                out_fh.close()
                err_fh.close()
            job = Job(
                job_id=job_id, command=command, cwd=cwd, proc=proc,
                deadline_epoch=deadline_epoch, sandboxed=sandboxed,
                detached=detached, pid=proc.pid, pgid=proc.pid,
                proc_starttime=_proc_starttime(proc.pid),
                stdout_path=stdout_path, stderr_path=stderr_path,
            )
            with self._lock:
                self._jobs[job.job_id] = job
                self._order.append(job.job_id)
            self._persist_insert(job)
            threading.Thread(
                target=self._waiter, args=(job,),
                name=f"hydra-job-{job.job_id}-wait", daemon=True).start()
            return job

        # In-memory mode (v0.2 behaviour, unchanged).
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            text=True,
            errors="replace",
            bufsize=1,
        )
        job = Job(
            job_id=job_id, command=command, cwd=cwd, proc=proc,
            deadline_epoch=deadline_epoch, sandboxed=sandboxed,
            detached=detached, pid=proc.pid, pgid=proc.pid,
            proc_starttime=_proc_starttime(proc.pid),
        )
        with self._lock:
            self._jobs[job.job_id] = job
            self._order.append(job.job_id)
        threading.Thread(
            target=self._drain, args=(job, proc.stdout, "stdout"),
            name=f"hydra-job-{job.job_id}-out", daemon=True).start()
        threading.Thread(
            target=self._drain, args=(job, proc.stderr, "stderr"),
            name=f"hydra-job-{job.job_id}-err", daemon=True).start()
        threading.Thread(
            target=self._waiter, args=(job,),
            name=f"hydra-job-{job.job_id}-wait", daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is not None:
            self._refresh_liveness(job)
        return job

    def list_jobs(self) -> list[dict]:
        with self._lock:
            jobs = [self._jobs[jid] for jid in self._order
                    if jid in self._jobs]
        for job in jobs:
            self._refresh_liveness(job)
        return [j.summary() for j in jobs]

    def counts(self) -> dict[str, int]:
        """(running, finished) counts for /health."""
        with self._lock:
            jobs = list(self._jobs.values())
        running = 0
        for job in jobs:
            self._refresh_liveness(job)
            with job._lock:
                if job.status == "running":
                    running += 1
        return {"running": running, "finished": len(jobs) - running}

    def kill(self, job_id: str) -> tuple[bool, Job | None]:
        """SIGKILL the job's whole process group. Returns
        (killed_anything, job). Works for re-attached jobs too:
        the group id is the recorded pid (children are session
        leaders, so pgid == pid)."""
        job = self.get(job_id)
        if job is None:
            return False, None
        with job._lock:
            if job.status != "running":
                return False, job
            job.status = "killed"
            job.finished_at = time.time()
        killed = self._kill_group(job)
        self._persist_update(job)
        return killed, job

    def kill_all(self) -> int:
        """Kill every running NON-detached job (used on daemon
        shutdown). Detached jobs are deliberately left running:
        surviving the daemon is their whole point."""
        with self._lock:
            jobs = list(self._jobs.values())
        killed = 0
        for job in jobs:
            if job.detached:
                continue
            done, _ = self.kill(job.job_id)
            killed += 1 if done else 0
        return killed

    def stop(self) -> None:
        self._reaper_stop.set()

    def close(self) -> None:
        """Stop the reaper and close the jobs database."""
        self.stop()
        with self._db_lock:
            db, self._db = self._db, None
        if db is not None:
            try:
                db.close()
            except sqlite3.Error:
                pass

    # -- internals -----------------------------------------------------

    def _refresh_liveness(self, job: Job) -> None:
        """For re-attached jobs (no Popen object): if the process is
        gone, finalise the record. Live jobs are left to their
        waiter thread."""
        if job.proc is not None:
            return
        with job._lock:
            if job.status != "running":
                return
            pid = job.pid
            starttime = job.proc_starttime
        if pid is None or not _pid_alive(pid, starttime):
            with job._lock:
                if job.status != "running":
                    return
                job.status = "done"
                job.returncode = None  # unknown: not our child
                job.finished_at = time.time()
            self._persist_update(job)

    def _prune_locked(self) -> None:
        """Drop oldest finished jobs when the table exceeds the cap,
        deleting their database rows and log files. Never prunes a
        running job."""
        while len(self._order) > self._max_jobs:
            oldest_id = self._order[0]
            oldest = self._jobs.get(oldest_id)
            if oldest is not None and oldest.status == "running":
                break  # never prune a running job
            self._order.pop(0)
            removed = self._jobs.pop(oldest_id, None)
            self._persist_delete(oldest_id)
            if removed is not None:
                for path in (removed.stdout_path, removed.stderr_path):
                    if path is not None:
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            pass

    def _drain(self, job: Job, stream, name: str) -> None:
        # In-memory mode only. Persistent jobs write to files directly.
        try:
            for chunk in iter(lambda: stream.read(65536), ""):
                if chunk:
                    job._append(name, chunk)
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _waiter(self, job: Job) -> None:
        assert job.proc is not None
        try:
            rc = job.proc.wait()
        except Exception:
            rc = None
        with job._lock:
            if job.returncode is None:
                job.returncode = rc
            if job.status == "running":
                job.status = "done"
                job.finished_at = time.time()
        self._persist_update(job)

    def _kill_group(self, job: Job) -> bool:
        """SIGKILL the job's process group. Returns True when a
        signal was (probably) delivered."""
        pgid = job.pgid if job.pgid is not None else job.pid
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGKILL)
                return True
            except (ProcessLookupError, PermissionError):
                pass
        if job.proc is not None:
            try:
                job.proc.kill()
                return True
            except Exception:
                pass
        elif job.pid is not None:
            try:
                os.kill(job.pid, signal.SIGKILL)
                return True
            except OSError:
                pass
        return False

    def _reap_loop(self) -> None:
        """Enforce per-job deadlines and refresh re-attached jobs.

        A job past its deadline is killed and marked timed_out (same
        semantics as /exec). Re-attached jobs found dead are
        finalised. Runs once a second.
        """
        while not self._reaper_stop.wait(REAPER_INTERVAL_SECONDS):
            now = time.time()
            with self._lock:
                jobs = list(self._jobs.values())
            for job in jobs:
                self._refresh_liveness(job)
                if job.deadline_epoch is None:
                    continue
                with job._lock:
                    if job.status != "running" or now < job.deadline_epoch:
                        continue
                    job.status = "killed"
                    job.timed_out = True
                    job.finished_at = time.time()
                self._kill_group(job)
                self._persist_update(job)
