"""HYDRA background jobs: long-running commands with poll/kill/output.

A job is a shell command started by the daemon (through the SAME
blocklist and jail checks as the synchronous /exec endpoint) whose
output the caller retrieves later with /jobs/poll. Jobs live only in
the daemon's memory: restarting the daemon forgets them, and the
shutdown endpoint kills any that are still running.

NO model code, NO network calls, NO telemetry. Output is capped the
same way as /exec (200KB per stream); anything beyond that is
discarded and reported as truncated.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

MAX_JOBS = 32
MAX_OUTPUT_BYTES = 200 * 1024  # per stream, same as synchronous /exec
REAPER_INTERVAL_SECONDS = 1.0


@dataclass
class Job:
    job_id: str
    command: str
    cwd: Path
    proc: subprocess.Popen
    deadline: float | None  # monotonic seconds, or None for no timeout
    sandboxed: bool = False
    status: str = "running"  # running | done | killed
    returncode: int | None = None
    timed_out: bool = False
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    _stdout_chunks: list[str] = field(default_factory=list, repr=False)
    _stderr_chunks: list[str] = field(default_factory=list, repr=False)
    _stdout_bytes: int = 0
    _stderr_bytes: int = 0
    _truncated_out: bool = False
    _truncated_err: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

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

    def snapshot(self, stdout_offset: int = 0) -> dict:
        """Current state + captured output (stdout from ``offset``)."""
        with self._lock:
            stdout = "".join(self._stdout_chunks)
            stderr = "".join(self._stderr_chunks)
            truncated = self._truncated_out or self._truncated_err
            status = self.status
            returncode = self.returncode
            timed_out = self.timed_out
            finished_at = self.finished_at
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
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class JobManager:
    """Thread-safe registry of background jobs."""

    def __init__(self, max_jobs: int = MAX_JOBS):
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []  # insertion order, for pruning
        self._lock = threading.Lock()
        self._max_jobs = max_jobs
        self._reaper_stop = threading.Event()
        self._reaper = threading.Thread(
            target=self._reap_loop, name="hydra-job-reaper", daemon=True)
        self._reaper.start()

    # -- lifecycle -----------------------------------------------------

    def start(
        self,
        argv: list[str],
        command: str,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: int | None,
        sandboxed: bool = False,
    ) -> Job:
        """Start a job. ``argv`` is already built by the caller (the
        daemon), after blocklist + jail checks. Raises RuntimeError
        when the job table is full of running jobs."""
        with self._lock:
            self._prune_locked()
            running = sum(
                1 for j in self._jobs.values() if j.status == "running")
            if running >= self._max_jobs:
                raise RuntimeError(
                    f"too many running jobs ({self._max_jobs}); "
                    "wait for one to finish or kill it first")
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
        deadline = (
            time.monotonic() + timeout_seconds
            if timeout_seconds and timeout_seconds > 0 else None
        )
        job = Job(
            job_id=uuid.uuid4().hex[:16],
            command=command,
            cwd=cwd,
            proc=proc,
            deadline=deadline,
            sandboxed=sandboxed,
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
            return self._jobs.get(job_id)

    def list_jobs(self) -> list[dict]:
        with self._lock:
            jobs = [self._jobs[jid] for jid in self._order
                    if jid in self._jobs]
        return [j.summary() for j in jobs]

    def kill(self, job_id: str) -> tuple[bool, Job | None]:
        """SIGKILL the job's whole process group. Returns
        (killed_anything, job)."""
        job = self.get(job_id)
        if job is None:
            return False, None
        with job._lock:
            if job.status != "running":
                return False, job
            job.status = "killed"
            job.finished_at = time.time()
        self._kill_group(job)
        return True, job

    def kill_all(self) -> int:
        """Kill every running job (used on daemon shutdown)."""
        with self._lock:
            jobs = list(self._jobs.values())
        killed = 0
        for job in jobs:
            done, _ = self.kill(job.job_id)
            killed += 1 if done else 0
        return killed

    def stop(self) -> None:
        self._reaper_stop.set()

    # -- internals -----------------------------------------------------

    def _prune_locked(self) -> None:
        """Drop oldest finished jobs when the table exceeds the cap."""
        while len(self._order) > self._max_jobs:
            oldest_id = self._order[0]
            oldest = self._jobs.get(oldest_id)
            if oldest is not None and oldest.status == "running":
                break  # never prune a running job
            self._order.pop(0)
            self._jobs.pop(oldest_id, None)

    def _drain(self, job: Job, stream, name: str) -> None:
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

    def _kill_group(self, job: Job) -> None:
        try:
            os.killpg(job.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                job.proc.kill()
            except Exception:
                pass

    def _reap_loop(self) -> None:
        """Enforce per-job deadlines: a job past its deadline is
        killed and marked timed_out (same semantics as /exec)."""
        while not self._reaper_stop.wait(REAPER_INTERVAL_SECONDS):
            now = time.monotonic()
            with self._lock:
                jobs = list(self._jobs.values())
            for job in jobs:
                if job.deadline is None:
                    continue
                with job._lock:
                    if job.status != "running" or now < job.deadline:
                        continue
                    job.status = "killed"
                    job.timed_out = True
                    job.finished_at = time.time()
                self._kill_group(job)
