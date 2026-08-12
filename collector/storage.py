"""Persistance SQLite.

Pourquoi SQLite et pas PostgreSQL : un seul writer, local, pas de réseau,
pas de concurrence. SQLite en WAL encaisse 1 Hz sans transpirer et le fichier
unique se copie/versionne trivialement. Postgres ici serait de la complexité
gratuite — et savoir le justifier vaut mieux que de l'utiliser par réflexe.
"""

from __future__ import annotations

import logging
import pathlib
import re
import sqlite3

log = logging.getLogger(__name__)

#: Les noms de colonnes viennent de labels sysfs (`temp1_label`, zones RAPL,
#: index de coeurs). Ils traversent forcément une f-string pour arriver dans un
#: ALTER TABLE — SQLite ne sait pas paramétrer un identifiant. On valide donc
#: en amont : c'est la seule barrière entre un label matériel exotique et une
#: DDL malformée.
IDENTIFIER_RE = re.compile(r"[a-z_][a-z0-9_]{0,62}")

COLUMN_TYPES = frozenset({"REAL", "INTEGER", "TEXT"})


def check_identifier(name: str) -> str:
    if not IDENTIFIER_RE.fullmatch(name):
        raise ValueError(f"nom de colonne invalide: {name!r}")
    return name


class Storage:
    def __init__(self, path: str | pathlib.Path, schema: str | pathlib.Path):
        path = pathlib.Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.executescript(pathlib.Path(schema).read_text())
        self._migrate()
        self._columns: set[str] = self._existing_columns()

    def _migrate(self) -> None:
        """Rattrape les bases créées par une version antérieure du schéma.

        CREATE TABLE IF NOT EXISTS ne touche pas une table existante : sans ça,
        une base de 14 jours ouverte par une version plus récente perdrait la
        déduplication des events sans le dire.
        """
        columns = {r[1] for r in self.conn.execute("PRAGMA table_info(events)")}
        if "dedup" not in columns:
            self.conn.execute("ALTER TABLE events ADD COLUMN dedup TEXT")
            log.info("migration: events.dedup ajoutée")
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_dedup"
            " ON events(dedup) WHERE dedup IS NOT NULL"
        )

    def _existing_columns(self) -> set[str]:
        rows = self.conn.execute("PRAGMA table_info(samples)").fetchall()
        return {r[1] for r in rows}

    def ensure_columns(self, fields: list[str], types: dict[str, str] | None = None) -> None:
        """Ajoute les colonnes manquantes. ALTER TABLE ADD COLUMN est O(1) en SQLite.

        Permet d'ajouter un reader (it87 par exemple) sans casser la base
        existante : les anciennes lignes auront simplement NULL.
        """
        types = types or {}
        for field in fields:
            check_identifier(field)
            if field in self._columns:
                continue
            coltype = types.get(field, "REAL")
            if coltype not in COLUMN_TYPES:
                raise ValueError(f"type de colonne invalide: {coltype!r}")
            self.conn.execute(f'ALTER TABLE samples ADD COLUMN "{field}" {coltype}')
            self._columns.add(field)
            log.info("colonne ajoutée: %s %s", field, coltype)

    def register_boot(self, boot_id: str, ts: int, kernel: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO boots (boot_id, ts_first, kernel) VALUES (?, ?, ?)",
            (boot_id, ts, kernel),
        )

    def insert_samples(self, rows: list[dict]) -> int:
        """Insert par batch. Un commit par seconde userait le SSD pour rien.

        Retourne le nombre de lignes écrites. Les clés inconnues de `samples`
        sont écartées : mieux vaut perdre une métrique qu'un batch entier sur
        une `sqlite3.OperationalError: no such column`.
        """
        if not rows:
            return 0
        cols = sorted({k for row in rows for k in row} & self._columns)
        if not cols:
            return 0
        dropped = {k for row in rows for k in row} - self._columns
        if dropped:
            log.warning("colonnes inconnues ignorées: %s", ", ".join(sorted(dropped)))

        quoted = ",".join(f'"{c}"' for c in cols)
        placeholders = ",".join("?" * len(cols))
        sql = f"INSERT OR REPLACE INTO samples ({quoted}) VALUES ({placeholders})"
        self.conn.execute("BEGIN")
        try:
            self.conn.executemany(sql, [[row.get(c) for c in cols] for row in rows])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return len(rows)




    # -- labels : l'annotation manuelle des expériences ---------------------




    def count_samples(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0])

    def close(self) -> None:
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        self.conn.close()
