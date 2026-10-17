"""Kern van `AuthStore`: bestanden, de verbinding per aanroep en de transactie.

`AuthStore` (`store`) is één object, per onderdeel over modules verdeeld: `_StoreCore` (hier)
-> `_GrantStore` (`_grants`, autorisatiecodes en tokens) -> `AuthStore` (`store`, gebruiker,
clients, lopende verzoeken en onderhoud). Elk deel gebruikt alleen wat eronder ligt. Het ontwerp
(verbinding, lock, bestandsmodi, hashing) staat in de module-docstring van `store`.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

from ._schema import migrate


class _StoreCore:
    """Zie de module-docstring."""

    def __init__(self, path: Path, clock: Callable[[], float]) -> None:
        self._path = Path(path)
        self._clock = clock
        self._lock = threading.RLock()
        self._prepare_files()
        with self._tx() as conn:
            migrate(conn)

    # --- infrastructuur ------------------------------------------------------------------

    def _prepare_files(self) -> None:
        parent = self._path.parent
        if not parent.exists():
            parent.mkdir(parents=True, mode=0o700)
            # mkdir past de modus aan op umask; forceer 0700 op de nieuwe map zelf.
            with contextlib.suppress(OSError):
                parent.chmod(0o700)
        if not self._path.exists():
            fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
        with contextlib.suppress(OSError):
            self._path.chmod(0o600)

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self._path, timeout=5.0, isolation_level=None)
            try:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                else:
                    conn.execute("COMMIT")
            finally:
                conn.close()

    def schema_version(self) -> int:
        with self._tx() as conn:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
