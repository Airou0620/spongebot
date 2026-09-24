"""Immutable filename snapshots, refreshed independently of bot requests."""
from __future__ import annotations

import atexit
import logging
import math
import threading
import weakref
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    memes: tuple[str, ...] = ()
    yn: tuple[str, ...] = ()
    airou: tuple[str, ...] = ()
    generation: int = 0


_active_indexes: weakref.WeakSet[FilenameIndex] = weakref.WeakSet()


def _stop_indexes() -> None:
    # Daemon workers only LIST objects; shutdown never waits on an S3 outage.
    for index in list(_active_indexes):
        index.close(wait=False)


atexit.register(_stop_indexes)


class FilenameIndex:
    """One published generation; readers never acquire the refresh lock or do I/O.

    Warm explicitly with refresh(), then start() one background worker. The worker
    holds only a weak reference while sleeping, so discarded storage clients do
    not live forever. A failed load leaves the entire previous generation intact.
    """

    def __init__(
        self,
        loader: Callable[[], CatalogSnapshot],
        interval: float = 900.0,
    ) -> None:
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("S3_INDEX_REFRESH_SECONDS must be finite and > 0")
        self._loader = loader
        self.interval = interval
        self._snapshot: CatalogSnapshot | None = None
        self._refresh_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def snapshot(self) -> CatalogSnapshot:
        snapshot = self._snapshot
        if snapshot is None:
            raise RuntimeError("Filename index is not initialized; call refresh() first")
        return snapshot

    def refresh(self) -> CatalogSnapshot:
        """Startup/manual/worker-only I/O. No reader calls this method."""
        with self._refresh_lock:
            if self._stop.is_set():
                raise RuntimeError("Filename index is closed")
            loaded = self._loader()
            old = self._snapshot
            # Copy mutable input defensively; normal loaders already return tuples.
            memes, yn, airou = tuple(loaded.memes), tuple(loaded.yn), tuple(loaded.airou)
            if old is not None:
                # Unchanged directories retain the same tuple/string objects.
                if memes == old.memes:
                    memes = old.memes
                if yn == old.yn:
                    yn = old.yn
                if airou == old.airou:
                    airou = old.airou
            new = CatalogSnapshot(
                memes=memes, yn=yn, airou=airou,
                generation=1 if old is None else old.generation + 1,
            )
            if self._stop.is_set():
                raise RuntimeError("Filename index closed during refresh")
            # Publish all three directories at once, after ALL pages succeed.
            self._snapshot = new
            return new

    def start(self) -> None:
        """Start once, without reloading the warm index or starting another thread."""
        with self._lifecycle_lock:
            if self._stop.is_set():
                raise RuntimeError("Filename index is closed")
            self.snapshot  # Fail early if callers forgot the initial warmup.
            if self._thread is not None and self._thread.is_alive():
                return
            thread = threading.Thread(
                name="s3-filename-refresh",
                target=_refresh_worker,
                args=(weakref.ref(self), self._stop, self.interval),
                daemon=True,
            )
            self._thread = thread
            _active_indexes.add(self)
            thread.start()

    def close(self, *, wait: bool = True) -> bool:
        """Stop scheduling refreshes; bound the wait for an in-flight S3 request."""
        with self._lifecycle_lock:
            self._stop.set()
            thread = self._thread
        if wait and thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        _active_indexes.discard(self)
        return thread is None or not thread.is_alive()


def _refresh_worker(
    ref: weakref.ReferenceType[FilenameIndex],
    stop: threading.Event,
    interval: float,
) -> None:
    # Wait AFTER each completed refresh: slow scans cannot queue overlapping work.
    while not stop.wait(interval):
        index = ref()
        if index is None:
            return
        try:
            snapshot = index.refresh()
            logger.info(
                "[Index] refreshed generation=%d memes=%d yn=%d airou=%d",
                snapshot.generation, len(snapshot.memes), len(snapshot.yn),
                len(snapshot.airou),
            )
        except Exception:
            if not stop.is_set():
                logger.warning(
                    "[Index] refresh failed; keeping last successful filename snapshot",
                    exc_info=True,
                )
        finally:
            # Do not retain a catalog generation while this worker sleeps.
            snapshot = None
            del index
