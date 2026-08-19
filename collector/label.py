"""Annotation manuelle des expériences.

Le collector produit des séries ; ce module produit la seule chose qu'aucun
capteur ne donne : la vérité terrain. Sans elle on n'a qu'un détecteur
non supervisé qu'on ne peut pas évaluer.

    python -m collector.label start airflow_blocked -n "grille avant obstruée"
    python -m collector.label list
    python -m collector.label end

`end` sans argument ferme tous les labels ouverts — le cas courant après une
expérience où l'on a oublié de noter l'id.
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib

from .main import SCHEMA, now_ms
from .storage import Storage

#: Indicatif, pas contraignant : un label hors liste est accepté (avec un
#: avertissement). Figer le vocabulaire trop tôt empêcherait de nommer
#: l'anomalie qu'on n'avait pas prévue — qui est justement celle qui compte.
KNOWN_LABELS = (
    "normal",
    "fan_curve_low",
    "airflow_blocked",
    "oc_unstable",
    "stress_ng",
    "gaming",
)


def human(ts_ms: int | None) -> str:
    if ts_ms is None:
        return "en cours"
    return dt.datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def main() -> None:
    ap = argparse.ArgumentParser(description="Annotation des expériences (table labels)")
    ap.add_argument("--db", default="data/metrics.db")
    ap.add_argument("--schema", type=pathlib.Path, default=SCHEMA)
    sub = ap.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="ouvrir une fenêtre labellisée")
    start.add_argument("label")
    start.add_argument("-n", "--notes", default="")

    end = sub.add_parser("end", help="fermer une fenêtre (toutes si id omis)")
    end.add_argument("id", nargs="?", type=int)

    sub.add_parser("list", help="lister les fenêtres ouvertes")

    args = ap.parse_args()
    storage = Storage(args.db, args.schema)
    try:
        if args.command == "start":
            if args.label not in KNOWN_LABELS:
                print(f"note: '{args.label}' hors liste ({', '.join(KNOWN_LABELS)})")
            label_id = storage.start_label(now_ms(), args.label, args.notes)
            print(f"label {label_id} ouvert: {args.label}")
        elif args.command == "end":
            closed = storage.end_label(now_ms(), args.id)
            print(f"{closed} label(s) fermé(s)")
        else:
            rows = storage.open_labels()
            if not rows:
                print("aucun label ouvert")
            for row in rows:
                print(f"#{row[0]}  {human(row[1])}  {row[2]}  {row[3]}")
    finally:
        storage.close()


if __name__ == "__main__":
    main()
