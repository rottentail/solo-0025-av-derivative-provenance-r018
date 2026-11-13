"""Thread-safe, content-addressed cache for intermediate media stages.

A *stage* is the deterministic result of applying a prefix of a normalized
operation plan to specific source bytes.  Stage keys capture every input that
can change the result:

* the SHA-256 digest of the primary source bytes actually read,
* the SHA-256 digest of the secondary source bytes actually read (once a mix
  has consumed the secondary, and ``None`` before that point),
* the full normalized operations in the prefix, including every numeric
  parameter.

Two renditions therefore share a stage only when their sources and the exact
applied operations match.  A different secondary mix can never reuse a stage
keyed to another secondary asset, even if the primary asset and operation
names are identical.

Concurrency rules:

* Stages are stored only after their computation finishes completely.  A
  failed computation is evicted, so concurrent requests can never observe a
  half-written stage.
* Each key has its own lock.  Requests for different keys run fully in
  parallel; only identical keys briefly coordinate, which keeps genuine
  parallelism instead of serializing all rendering.
* The cache never mutates a stored object; callers receive the same immutable
  clip object (the clip dataclasses are frozen).
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, TypeVar

from .metrics import StageMetrics

T = TypeVar("T")


class StageCache:
    def __init__(self, scope: str, metrics: StageMetrics | None = None) -> None:
        self.scope = scope
        self._entries: dict[Any, Any] = {}
        self._key_locks: dict[Any, threading.Lock] = {}
        self._guard = threading.Lock()
        self.metrics = metrics or StageMetrics(scope)

    def _lock_for(self, key: Any) -> threading.Lock:
        with self._guard:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[key] = lock
            return lock

    def get_or_compute(
        self,
        key: Any,
        stage: str,
        compute: Callable[[], T],
    ) -> T:
        """Return the cached stage or compute it once under a per-key lock."""
        with self._guard:
            cached = self._entries.get(key)
        if cached is not None:
            started = time.perf_counter()
            self.metrics.record_reuse(stage, time.perf_counter() - started)
            return cached

        key_lock = self._lock_for(key)
        started = time.perf_counter()
        with key_lock:
            # Another request may have finished this exact stage while we
            # waited for its lock.
            with self._guard:
                cached = self._entries.get(key)
            if cached is not None:
                self.metrics.record_reuse(stage, time.perf_counter() - started)
                return cached

            compute_started = time.perf_counter()
            try:
                result = compute()
            except BaseException:
                # Never poison the cache with a partial or failed stage.
                with self._guard:
                    self._entries.pop(key, None)
                self.metrics.record_failure(stage, time.perf_counter() - compute_started)
                raise
            with self._guard:
                self._entries[key] = result
            self.metrics.record_compute(stage, time.perf_counter() - compute_started)
            return result

    def get(self, key: Any) -> Any | None:
        with self._guard:
            return self._entries.get(key)

    def __len__(self) -> int:
        with self._guard:
            return len(self._entries)
