"""Werkthreads van de bridge (`flipper-ui-action`, `-confirm`, `-device`, `-aux`).

Een lane voert alleen uit wat de hoofdlus erin zet; uitkomsten gaan terug via de inbox van de
hoofdlus (zie de module-docstring van `bridge.py`).
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable

from pitoolbox.frontends.flipper_ui.bridge_types import LOG_NAME

log = logging.getLogger(LOG_NAME)


class _Lane:
    """Eén werkthread met een begrensde FIFO."""

    def __init__(self, name: str, maxsize: int = 16) -> None:
        self.name = name
        self._q: queue.Queue[Callable[[], None]] = queue.Queue(maxsize)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._main, name=name, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def submit(self, fn: Callable[[], None]) -> bool:
        try:
            self._q.put_nowait(fn)
            return True
        except queue.Full:
            log.warning("%s: wachtrij vol, taak overgeslagen", self.name)
            return False

    def stop(self, timeout: float) -> None:
        self._stop.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout)

    def _main(self) -> None:
        while not self._stop.is_set():
            try:
                fn = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                fn()
            except Exception:
                log.exception("%s: taak faalde", self.name)
