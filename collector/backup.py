"""Copie cohérente d'une base SQLite active, y compris son WAL validé."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from contextlib import closing
import math
import pathlib
import sqlite3
import time

from .database import read_snapshot


def backup_database(
    db: str | pathlib.Path,
    destination: str | pathlib.Path,
    *,
    timeout: float = 30.0,
) -> pathlib.Path:
    """Crée une nouvelle sauvegarde autonome sans arrêter le collecteur.

    L'API backup de SQLite inclut les transactions validées dans le WAL.
    La destination doit être absente et une copie abandonnée est supprimée.
    Les données encore dans les buffers RAM du collecteur ne sont pas incluses.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("le délai de sauvegarde doit être un nombre fini > 0")
    destination = pathlib.Path(destination)
    deadline = time.monotonic() + timeout

    def check_deadline(status, remaining, total):
        if time.monotonic() >= deadline:
            raise TimeoutError("délai de sauvegarde dépassé")

    # L'attente SQLite par défaut de 5 s masquerait les petits délais choisis
    # par l'utilisateur. L'API backup réessaie avec son propre callback borné.
    with read_snapshot(db, timeout=0.0) as source:
        # Création exclusive avant que SQLite ouvre le fichier : protège aussi
        # la source si elle a été choisie par erreur comme destination.
        destination.open("xb").close()
        try:
            with closing(sqlite3.connect(destination, isolation_level=None)) as target:
                source.backup(target, pages=256, progress=check_deadline, sleep=0.05)
                if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise sqlite3.DatabaseError("la copie SQLite ne passe pas quick_check")
                target.execute("PRAGMA journal_mode=DELETE")
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
    return destination


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Sauvegarder une base BAD pendant la collecte")
    parser.add_argument("--db", default="data/metrics.db", help="base SQLite existante")
    parser.add_argument("--output", required=True, type=pathlib.Path, help="nouveau fichier de sauvegarde")
    parser.add_argument("--timeout", type=float, default=30.0, help="délai de copie en secondes (défaut : 30)")
    args = parser.parse_args(argv)
    try:
        destination = backup_database(args.db, args.output, timeout=args.timeout)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.error(f"sauvegarde impossible: {exc}")
    print(f"sauvegarde créée : {destination}")


if __name__ == "__main__":
    main()
