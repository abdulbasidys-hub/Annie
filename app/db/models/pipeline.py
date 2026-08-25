"""One record per pipeline stage execution — manual "Run now" clicks and
scheduled jobs alike (§20, §2026-08-25).

Firestore layout: ``pipeline_runs/{auto_id}``. Exists because the previous
design had the operator's browser hold the only record of a run's outcome —
the HTTP request blocked until the stage finished, so a slow batch (creator
wallet lookups can each take seconds) looked identical to a hung one, and
navigating away lost the result entirely. Now every run (manual or
scheduled) gets a real row the moment it starts, updated in place when it
finishes, so "Run now" can return immediately with an id to poll and the
page can show real history instead of nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class PipelineRun:
    id: str = ""
    stage: str = ""  # discovery | enrichment | trends | narratives | full_cycle
    trigger: str = "manual"  # manual | scheduled
    status: str = "running"  # running | done | error
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
