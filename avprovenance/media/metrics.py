"""Process-local counters that prove shared media stages reduce real work.

The counters are deliberately independent of the media implementation: every
stage computation records whether it was freshly computed or reused, how long
it took, and which logical pipeline (production rendering versus an integrity
round) it belonged to.  Reading the snapshot never blocks a computation.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass
class StageMetrics:
    """Accumulated execution counts and wall time for one cache scope."""

    scope: str
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    computed: int = 0
    reused: int = 0
    failed: int = 0
    compute_seconds: float = 0.0
    wait_seconds: float = 0.0
    by_stage: dict[str, dict[str, int]] = field(default_factory=dict)

    def record_compute(self, stage: str, elapsed: float) -> None:
        with self._lock:
            self.computed += 1
            self.compute_seconds += elapsed
            bucket = self.by_stage.setdefault(
                stage,
                {"computed": 0, "reused": 0, "failed": 0},
            )
            bucket["computed"] += 1

    def record_reuse(self, stage: str, elapsed: float) -> None:
        with self._lock:
            self.reused += 1
            self.wait_seconds += elapsed
            bucket = self.by_stage.setdefault(
                stage,
                {"computed": 0, "reused": 0, "failed": 0},
            )
            bucket["reused"] += 1

    def record_failure(self, stage: str, elapsed: float) -> None:
        with self._lock:
            self.failed += 1
            self.compute_seconds += elapsed
            bucket = self.by_stage.setdefault(
                stage,
                {"computed": 0, "reused": 0, "failed": 0},
            )
            bucket["failed"] += 1

    @property
    def lookup_count(self) -> int:
        return self.computed + self.reused + self.failed

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "scope": self.scope,
                "computed": self.computed,
                "reused": self.reused,
                "failed": self.failed,
                "lookups": self.lookup_count,
                "compute_seconds": round(self.compute_seconds, 9),
                "wait_seconds": round(self.wait_seconds, 9),
                "reuse_ratio": (
                    round(self.reused / self.lookup_count, 6)
                    if self.lookup_count
                    else 0.0
                ),
                "by_stage": {
                    stage: dict(counts) for stage, counts in sorted(self.by_stage.items())
                },
            }


class MetricsRegistry:
    """Holds the production metrics and one live metrics object per round."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.production = StageMetrics("production")
        self._rounds: dict[int, StageMetrics] = {}

    def register_round(self, round_id: int) -> StageMetrics:
        metrics = StageMetrics(f"verification-round-{round_id}")
        with self._lock:
            self._rounds[round_id] = metrics
        return metrics

    def unregister_round(self, round_id: int) -> None:
        with self._lock:
            self._rounds.pop(round_id, None)

    def round_snapshots(self) -> list[dict[str, Any]]:
        with self._lock:
            return [metrics.snapshot() for _, metrics in sorted(self._rounds.items())]

    def snapshot(self) -> dict[str, Any]:
        return {
            "production": self.production.snapshot(),
            "active_verification_rounds": self.round_snapshots(),
        }
