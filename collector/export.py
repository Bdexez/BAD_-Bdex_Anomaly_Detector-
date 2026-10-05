"""Export CSV des mesures et de leurs annotations, sans modifier la base."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import closing
import csv
import json
import pathlib
import sqlite3
from typing import TextIO

from .storage import quote_identifier


def export_csv(
    db: str | pathlib.Path,
    target: TextIO,
) -> int:
    """Écrit une ligne par mesure, en flux, depuis un snapshot SQLite cohérent.

    Les fenêtres sont [début, fin[. labels_json conserve toutes les annotations
    qui se chevauchent, leurs notes et leurs bornes ; [] n'invente aucun label.
    Les anciennes bases sont lisibles sans déclencher leur migration.
    """
    uri = pathlib.Path(db).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, isolation_level=None)) as conn:
        conn.execute("BEGIN")
        fields = conn.execute("PRAGMA table_info(samples)").fetchall()
        available = [field[1] for field in fields]
        selected = available
        primary = [field[1] for field in fields if field[5]]
        if len(primary) != 1:
            raise ValueError("la table samples doit avoir une clé primaire unique")
        key = f"s.{quote_identifier(primary[0])}"
        projection = ", ".join(f"s.{quote_identifier(field)}" for field in selected)
        cursor = conn.execute(
            f"SELECT {key}, {projection}, l.id, l.ts_start, l.ts_end, l.label, l.notes"
            " FROM samples AS s LEFT JOIN labels AS l"
            " ON l.ts_start <= s.ts AND (l.ts_end IS NULL OR s.ts < l.ts_end)"
            + f" ORDER BY s.ts, {key}, l.id",
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
