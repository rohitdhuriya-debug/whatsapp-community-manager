"""In-process job registry for long generations.

Generating a PDF through Claude Code takes up to two minutes. A plain blocking
POST gives the browser nothing to show, so generation runs as a background task
that reports real phases, and the UI polls this registry for progress.

Progress is derived from actual phase transitions plus elapsed time inside the
current phase, so the bar reflects what is happening rather than animating on a
timer.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# Phase -> (start %, end %, typical seconds). The bar eases from start to end
# across the expected duration, then holds just short of `end` until the phase
# actually completes, so it never claims to be finished early.
PHASES: dict[str, tuple[int, int, float]] = {
    "queued":    (0, 3, 1.0),
    "research":  (3, 22, 12.0),
    "drafting":  (22, 72, 45.0),
    "building":  (72, 88, 6.0),
    "saving":    (88, 99, 2.0),
    "done":      (100, 100, 0.0),
}

# Free models vary hugely - some answer in 15s, some take two minutes - and
# Claude Code is slower still. Expecting too little pins the bar at its ceiling
# and looks stuck, so the drafting estimate is per engine and per output type.
DRAFTING_SECONDS = {
    ("openrouter", "message"): 30.0,
    ("openrouter", "poll"): 25.0,
    ("openrouter", "pdf"): 90.0,
    ("openrouter", "excel"): 75.0,
    ("claude_code", "message"): 35.0,
    ("claude_code", "poll"): 30.0,
    ("claude_code", "pdf"): 75.0,
    ("claude_code", "excel"): 60.0,
}

PHASE_LABELS = {
    "queued":   "Starting…",
    "research": "Searching the web for fresh sources…",
    "drafting": "Writing with {engine}…",
    "building": "Building the {kind} file…",
    "saving":   "Preparing the preview…",
    "done":     "Done",
}

# Finished jobs are kept briefly so a slow poll still sees the result.
RETAIN_SECONDS = 900


@dataclass
class Job:
    id: str
    kind: str = "message"
    engine: str = "openrouter"
    phase: str = "queued"
    status: str = "running"          # running | done | error
    result: dict[str, Any] | None = None
    error: str | None = None
    detail: str = ""
    started_at: float = field(default_factory=time.monotonic)
    phase_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None

    def set_phase(self, phase: str, detail: str = "") -> None:
        """Advance the phase, or just attach a detail note to the current one.

        The elapsed timer only restarts on a real phase change - otherwise a
        mid-phase note (like a model fallback) would rewind the progress bar.
        """
        if phase != self.phase:
            self.phase = phase
            self.phase_at = time.monotonic()
            log.info("Job %s -> %s", self.id[:8], phase)
        self.detail = detail

    @property
    def percent(self) -> int:
        if self.status == "done":
            return 100
        if self.status == "error":
            return 100
        start, end, expected = PHASES.get(self.phase, (0, 99, 30.0))
        if self.phase == "drafting":
            expected = DRAFTING_SECONDS.get((self.engine, self.kind), expected)
        if expected <= 0:
            return end

        elapsed = time.monotonic() - self.phase_at
        if elapsed <= expected:
            # Ease out to 85% of the phase span by the expected duration.
            ratio = 0.85 * (1 - (1 - elapsed / expected) ** 2)
        else:
            # Past the estimate, creep asymptotically instead of freezing, so a
            # slow model still looks like it is making progress.
            over = (elapsed - expected) / expected
            ratio = 0.85 + 0.15 * (1 - 1 / (1 + over))

        span = (end - start - 1) * ratio
        return int(min(start + span, end - 1))

    @property
    def label(self) -> str:
        if self.status == "error":
            return "Failed"
        template = PHASE_LABELS.get(self.phase, "Working…")
        engine = "Claude Code" if self.engine == "claude_code" else "the free model"
        return template.format(engine=engine, kind=self.kind.upper())

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "phase": self.phase,
            "percent": self.percent,
            "label": self.label,
            "detail": self.detail,
            "elapsed": round(time.monotonic() - self.started_at, 1),
            "result": self.result,
            "error": self.error,
        }


_JOBS: dict[str, Job] = {}


def create(kind: str, engine: str) -> Job:
    _purge()
    job = Job(id=uuid.uuid4().hex, kind=kind, engine=engine)
    _JOBS[job.id] = job
    return job


def get(job_id: str) -> Job | None:
    return _JOBS.get(job_id)


def finish(job: Job, result: dict[str, Any]) -> None:
    job.result = result
    job.status = "done"
    job.set_phase("done")
    job.finished_at = time.monotonic()


def fail(job: Job, message: str) -> None:
    job.error = message
    job.status = "error"
    job.finished_at = time.monotonic()
    log.warning("Job %s failed: %s", job.id[:8], message)


def _purge() -> None:
    now = time.monotonic()
    stale = [
        key for key, job in _JOBS.items()
        if job.finished_at is not None and (now - job.finished_at) > RETAIN_SECONDS
    ]
    for key in stale:
        _JOBS.pop(key, None)


def run(job: Job, coro) -> None:
    """Fire the coroutine in the background, recording success or failure."""

    async def _wrapper() -> None:
        try:
            result = await coro
        except Exception as exc:  # surfaced to the UI via the job record
            fail(job, str(exc) or type(exc).__name__)
        else:
            finish(job, result)

    asyncio.create_task(_wrapper())
