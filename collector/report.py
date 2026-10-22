"""Rapport de complétude du dataset, sans modifier ni migrer la base."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
import pathlib
import sqlite3

from .database import read_snapshot
from .storage import quote_identifier


def _percentage(part: int, total: int) -> float | None:
    return part / total * 100 if total else None


def dataset_report(db: str | pathlib.Path) -> dict:
    """Compte les valeurs présentes, les annotations et les événements.

    Les agrégations restent dans SQLite : aucune série de mesures n'est
    chargée en mémoire Python. La complétude ne préjuge pas de la validité
    physique d'une mesure, et aucune vérité terrain n'est inférée.
    """
    with read_snapshot(db) as conn:
        fields = conn.execute("PRAGMA table_info(samples)").fetchall()
        if not {"ts", "boot_id"} <= {field[1] for field in fields}:
            raise ValueError("la base ne contient pas de table samples compatible")
        metrics = [field for field in fields if field[1] not in ("sample_id", "ts", "boot_id")]
        count, first, last, labelled = conn.execute(
            "SELECT COUNT(*), MIN(s.ts), MAX(s.ts), COALESCE(SUM(EXISTS("
            " SELECT 1 FROM labels AS l WHERE l.ts_start <= s.ts"
            " AND (l.ts_end IS NULL OR s.ts < l.ts_end))), 0) FROM samples AS s"
        ).fetchone()
        counts = ()
        if metrics:
            counts = conn.execute(
                "SELECT " + ", ".join(f"COUNT({quote_identifier(field[1])})" for field in metrics)
                + " FROM samples"
            ).fetchone()
        boots = conn.execute(
            "SELECT boot_id, COUNT(*), MIN(ts), MAX(ts), COUNT(*) - COUNT(DISTINCT ts)"
            " FROM samples GROUP BY boot_id ORDER BY boot_id"
        ).fetchall()
        total_labels, open_labels = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(ts_end IS NULL), 0) FROM labels"
        ).fetchone()
        events = conn.execute(
            "SELECT source, kind, severity, COUNT(*) FROM events"
            " GROUP BY source, kind, severity ORDER BY source, kind, severity"
        ).fetchall()
        return {
            "samples": {"count": count, "first_ts": first, "last_ts": last,
                        "labelled": labelled, "labelled_pct": _percentage(labelled, count)},
            "boots": [{"boot_id": boot, "samples": n, "first_ts": start,
                       "last_ts": end, "duplicate_timestamps": duplicates}
                      for boot, n, start, end, duplicates in boots],
            "metrics": [{"column": field[1], "type": field[2], "present": present,
                         "missing": count - present, "coverage_pct": _percentage(present, count)}
                        for field, present in zip(metrics, counts)],
            "labels": {"total": total_labels, "open": open_labels},
            "events": [{"source": source, "kind": kind, "severity": severity, "count": n}
                       for source, kind, severity, n in events],
        }


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="État et complétude du dataset BAD")
    parser.add_argument("--db", default="data/metrics.db", help="base SQLite existante")
    parser.add_argument("--json", action="store_true", help="rapport JSON pour les scripts")
    args = parser.parse_args(argv)
    try:
        report = dataset_report(args.db)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.error(str(exc))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    samples = report["samples"]
    print(f"Mesures : {samples['count']} · boots : {len(report['boots'])}")
    print(f"Période (epoch ms) : {samples['first_ts']} → {samples['last_ts']}")
    coverage = samples["labelled_pct"]
    formatted = "indéfinie" if coverage is None else f"{coverage:.1f} %"
    print(f"Mesures annotées : {samples['labelled']}/{samples['count']} ({formatted})")
    print(f"Fenêtres : {report['labels']['total']} · ouvertes : {report['labels']['open']}")
    for boot in report["boots"]:
        print(f"  boot {boot['boot_id']} : {boot['samples']} mesures, "
              f"{boot['duplicate_timestamps']} horodatage(s) répété(s)")
    for metric in report["metrics"]:
        coverage = metric["coverage_pct"]
        formatted = "indéfinie" if coverage is None else f"{coverage:.1f} %"
        print(f"  {metric['column']} : {formatted} présente(s), {metric['missing']} manquante(s)")
    print(f"Événements : {sum(event['count'] for event in report['events'])}")
    for event in report["events"]:
        print(f"  {event['source']}/{event['kind']} ({event['severity']}) : {event['count']}")


if __name__ == "__main__":
    main()
