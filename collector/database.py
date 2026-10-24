"""Accès aux bases existantes pour les outils de lecture, sans migration."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing, contextmanager
import pathlib
import sqlite3


@contextmanager
def read_snapshot(path: str | pathlib.Path, *, timeout: float = 5.0) -> Iterator[sqlite3.Connection]:
    uri = pathlib.Path(path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, isolation_level=None, timeout=timeout)) as conn:
        conn.execute("BEGIN")
        yield conn
