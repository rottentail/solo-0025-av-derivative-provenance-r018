"""Scope integrity-check recomputation to a round of concurrent checks.

Production rendering and integrity verification must never share cached media:
a verification has to derive its result from the source bytes it reads in the
current round and cannot treat a production output or a prior audit as proof.

A *round* is the window in which integrity checks overlap.  The first check to
start opens a round with a brand-new, empty :class:`StageCache`; checks that
start while one is in flight join it and therefore reuse shared intermediate
stages computed from the same freshly read sources.  When the last concurrent
check finishes, the round -- and every stage it cached -- is discarded, so the
next round recomputes from source again rather than borrowing past results.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

from .media import MetricsRegistry, StageCache


class VerificationRoundManager:
    def __init__(self, metrics_registry: MetricsRegistry) -> None:
        self._registry = metrics_registry
        self._lock = threading.Lock()
        self._in_flight = 0
        self._round_id = 0
        self._active_cache: StageCache | None = None

    @contextmanager
    def round_cache(self) -> Iterator[StageCache]:
        with self._lock:
            if self._in_flight == 0:
                self._round_id += 1
                round_id = self._round_id
                metrics = self._registry.register_round(round_id)
                self._active_cache = StageCache(
                    scope=f"verification-round-{round_id}",
                    metrics=metrics,
                )
            self._in_flight += 1
            cache = self._active_cache
        assert cache is not None
        try:
            yield cache
        finally:
            with self._lock:
                self._in_flight -= 1
                if self._in_flight == 0:
                    # Drop every stage so a later round cannot reuse old proof.
                    self._active_cache = None
                    self._registry.unregister_round(self._round_id)

    @property
    def active_round_id(self) -> int | None:
        with self._lock:
            return self._round_id if self._in_flight else None
