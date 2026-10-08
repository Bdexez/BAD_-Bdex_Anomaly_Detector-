"""Export CSV des mesures et de leurs annotations, sans modifier la base."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from contextlib import closing, contextmanager
import csv
import json
import pathlib
import sqlite3
import sys
from typing import TextIO

from .storage import quote_identifier


def export_csv(
    db: str | pathlib.Path,
    target: TextIO,
    *,
    boot_id: str | None = None,
    since: int | None = None,
    until: int | None = None,
    columns: Sequence[str] | None = None,
) -> int:
    """Écrit une ligne par mesure, en flux, depuis un snapshot SQLite cohérent.

    Les fenêtres sont [début, fin[. labels_json conserve toutes les annotations
    qui se chevauchent, leurs notes et leurs bornes ; [] n'invente aucun label.
    Les anciennes bases sont lisibles sans déclencher leur migration.
    """
    if since is not None and until is not None and until <= since:
        raise ValueError("--until doit être supérieur à --since")
    uri = pathlib.Path(db).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, isolation_level=None)) as conn:
        conn.execute("BEGIN")
        fields = conn.execute("PRAGMA table_info(samples)").fetchall()
        available = [field[1] for field in fields]
        selected = available if columns is None else list(columns)
        if not selected:
            raise ValueError("aucune colonne de mesures à exporter")
        unknown = set(selected) - set(available)
        if unknown:
            raise ValueError(f"colonnes inconnues: {', '.join(sorted(unknown))}")
        if len(set(selected)) != len(selected) or "labels_json" in selected:
            raise ValueError("colonnes dupliquées ou nom réservé: labels_json")
        primary = [field[1] for field in fields if field[5]]
        if len(primary) != 1:
            raise ValueError("la table samples doit avoir une clé primaire unique")
        key = f"s.{quote_identifier(primary[0])}"
        projection = ", ".join(f"s.{quote_identifier(field)}" for field in selected)
        filters = []
        params: list[str | int] = []
        for value, condition in (
            (boot_id, "s.boot_id = ?"), (since, "s.ts >= ?"), (until, "s.ts < ?"),
        ):
            if value is not None:
                filters.append(condition)
                params.append(value)
        where = " WHERE " + " AND ".join(filters) if filters else ""
        cursor = conn.execute(
            f"SELECT {key}, {projection}, l.id, l.ts_start, l.ts_end, l.label, l.notes"
            " FROM samples AS s LEFT JOIN labels AS l"
            " ON l.ts_start <= s.ts AND (l.ts_end IS NULL OR s.ts < l.ts_end)"
            + where + f" ORDER BY s.ts, {key}, l.id",
            params,
        )
        writer = csv.writer(target, lineterminator="\n")
        writer.writerow([*selected, "labels_json"])
        count = 0
        previous = None
        values: tuple = ()
        labels: list[dict] = []

        def write_sample() -> None:
            writer.writerow([*values, json.dumps(labels, ensure_ascii=False, separators=(",", ":"))])

        for row in cursor:
            if row[0] != previous:
                if previous is not None:
                    write_sample()
                    count += 1
                previous = row[0]
                values = row[1:1 + len(selected)]
                labels = []
            label_id, start, end, label, notes = row[1 + len(selected):]
            if label_id is not None:
                labels.append({"id": label_id, "label": label, "notes": notes,
                               "ts_start": start, "ts_end": end})
        if previous is not None:
            write_sample()
            count += 1
        return count


@contextmanager
def _output(destination: str):
    if destination == "-":
        yield sys.stdout
        return
    path = pathlib.Path(destination)
    stream = path.open("x", encoding="utf-8", newline="")
    try:
        with stream:
            yield stream
    except BaseException:
        # Seul le fichier créé par cet appel est supprimé en cas d'échec.
        # Un export partiel ne doit pas passer pour un dataset complet.
        path.unlink(missing_ok=True)
        raise


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Exporter les mesures et leurs labels en CSV")
    parser.add_argument("--db", default="data/metrics.db", help="base SQLite existante, ouverte en lecture seule")
    parser.add_argument("--output", default="-", help="nouveau fichier CSV, ou - pour stdout (défaut)")
    parser.add_argument("--boot", dest="boot_id", help="limiter à un boot_id")
    parser.add_argument("--since", type=int, help="epoch ms inclusif")
    parser.add_argument("--until", type=int, help="epoch ms exclusif")
    parser.add_argument("--columns", nargs="+", help="colonnes à exporter, dans l'ordre choisi")
    args = parser.parse_args(argv)
    try:
        with _output(args.output) as target:
            count = export_csv(args.db, target, boot_id=args.boot_id, since=args.since,
                               until=args.until, columns=args.columns)
            target.flush()
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.error(f"export impossible: {exc}")
    print(f"{count} mesure(s) exportée(s)", file=sys.stderr)


if __name__ == "__main__":
    main()
