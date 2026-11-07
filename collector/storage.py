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


def quote_identifier(name: str) -> str:
    """Échappe aussi les identifiants d'une base existante, sans les renommer."""
    return '"' + name.replace('"', '""') + '"'


class Storage:
    def __init__(self, path: str | pathlib.Path, schema: str | pathlib.Path):
        path = pathlib.Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, isolation_level=None)
        try:
            # Le schéma courant crée des index sur les nouvelles colonnes :
            # une base existante doit être migrée AVANT de l'exécuter.
            self._migrate()
            self.conn.executescript(pathlib.Path(schema).read_text())
            # Un ancien fichier de schéma peut aussi créer une base neuve.
            self._migrate()
            self._columns: set[str] = self._existing_columns()
        except BaseException:
            self.conn.close()
            raise

    def _migrate(self) -> None:
        """Rattrape les bases créées par une version antérieure du schéma.

        CREATE TABLE IF NOT EXISTS ne touche pas une table existante : sans ça,
        une base de 14 jours ouverte par une version plus récente perdrait la
        déduplication des events sans le dire.
        """
        self._migrate_samples()
        columns = {r[1] for r in self.conn.execute("PRAGMA table_info(events)")}
        if not columns:
            return
        if "dedup" not in columns:
            self.conn.execute("ALTER TABLE events ADD COLUMN dedup TEXT")
            log.info("migration: events.dedup ajoutée")
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_dedup"
            " ON events(dedup) WHERE dedup IS NOT NULL"
        )

    def _migrate_samples(self) -> None:
        """Sépare l'identité d'un sample de l'horloge murale, atomiquement.

        Les anciens ts étaient des clés primaires : deux lectures dans la
        même milliseconde, ou un retour de l'horloge, écrasaient une mesure.
        SQLite impose de reconstruire la table pour changer cette contrainte.
        """
        columns = self.conn.execute("PRAGMA table_info(samples)").fetchall()
        if not any(col[1] == "ts" and col[5] for col in columns):
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            # Un autre démarrage a pu migrer la base pendant l'attente du
            # verrou : ne jamais reconstruire depuis des métadonnées périmées.
            columns = self.conn.execute("PRAGMA table_info(samples)").fetchall()
            if not any(col[1] == "ts" and col[5] for col in columns):
                self.conn.execute("COMMIT")
                return
            if any(col[1] == "sample_id" for col in columns):
                raise ValueError("migration impossible: sample_id existe déjà comme métrique")
            definitions = ["sample_id INTEGER PRIMARY KEY"]
            for _, name, coltype, required, default, _ in columns:
                definition = f"{quote_identifier(name)} {coltype}"
                if required or name == "ts":
                    definition += " NOT NULL"
                if default is not None:
                    definition += f" DEFAULT {default}"
                definitions.append(definition)
            names = ", ".join(quote_identifier(col[1]) for col in columns)
            objects = self.conn.execute(
                "SELECT sql FROM sqlite_master WHERE tbl_name = 'samples'"
                " AND type IN ('index', 'trigger') AND sql IS NOT NULL"
            ).fetchall()
            self.conn.execute(f"CREATE TABLE _bad_samples_migration ({', '.join(definitions)})")
            self.conn.execute(
                f"INSERT INTO _bad_samples_migration ({names}) SELECT {names} FROM samples"
            )
            self.conn.execute("DROP TABLE samples")
            self.conn.execute("ALTER TABLE _bad_samples_migration RENAME TO samples")
            for (sql,) in objects:
                self.conn.execute(sql)
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        log.info("migration: samples.sample_id remplace ts comme clé primaire")

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
        cols = sorted(({k for row in rows for k in row} & self._columns) - {"sample_id"})
        if not cols:
            return 0
        dropped = {k for row in rows for k in row} - set(cols)
        if dropped:
            log.warning("colonnes inconnues ignorées: %s", ", ".join(sorted(dropped)))

        quoted = ",".join(f'"{c}"' for c in cols)
        placeholders = ",".join("?" * len(cols))
        sql = f"INSERT INTO samples ({quoted}) VALUES ({placeholders})"
        self.conn.execute("BEGIN")
        try:
            self.conn.executemany(sql, [[row.get(c) for c in cols] for row in rows])
            self.conn.execute("COMMIT")
        except BaseException:
            # Ctrl+C doit aussi libérer la transaction : l'arrêt du collector
            # retente ensuite le batch resté en RAM, sans insertions partielles.
            # Certaines erreurs SQLite annulent déjà la transaction entière.
            # Un second ROLLBACK masquerait alors l'erreur d'origine.
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        return len(rows)

    def add_event(
        self,
        ts: int,
        source: str,
        kind: str,
        severity: str = "",
        message: str = "",
        dedup: str | None = None,
    ) -> bool:
        """Enregistre un événement discret. Retourne False si c'est un doublon.

        `dedup` est une clé stable fournie par la source (numéro de séquence
        kmsg, curseur journald). Elle rend le rejeu du tampon noyau idempotent :
        redémarrer le collector trois fois ne crée pas trois fois la même MCE.
        """
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO events (ts, source, kind, severity, message, dedup)"
            " VALUES (?,?,?,?,?,?)",
            (ts, source, kind, severity, message[:4000], dedup),
        )
        return cur.rowcount > 0

    def recent_events(self, limit: int = 20) -> list[tuple]:
        return self.conn.execute(
            "SELECT ts, source, kind, severity, message FROM events"
            " ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def count_events_by_kind(self) -> list[tuple[str, str, int]]:
        return self.conn.execute(
            "SELECT source, kind, COUNT(*) FROM events GROUP BY source, kind"
            " ORDER BY COUNT(*) DESC"
        ).fetchall()

    # -- labels : l'annotation manuelle des expériences ---------------------

    def start_label(self, ts: int, label: str, notes: str = "") -> int:
        label = label.strip()
        if not label:
            raise ValueError("le label ne peut pas être vide")
        cur = self.conn.execute(
            "INSERT INTO labels (ts_start, label, notes) VALUES (?, ?, ?)",
            (ts, label, notes),
        )
        return int(cur.lastrowid)

    def end_label(self, ts: int, label_id: int | None = None) -> int:
        """Ferme un label ouvert (ou tous les labels ouverts si id absent)."""
        if label_id is not None and label_id <= 0:
            raise ValueError("l'identifiant du label doit être > 0")
        where = "ts_end IS NULL"
        params: tuple = ()
        if label_id is not None:
            where += " AND id = ?"
            params = (label_id,)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            invalid = self.conn.execute(
                f"SELECT id FROM labels WHERE {where} AND ts_start > ? LIMIT 1",
                (*params, ts),
            ).fetchone()
            if invalid is not None:
                raise ValueError(f"la fin précède le début du label #{invalid[0]}")
            cur = self.conn.execute(
                f"UPDATE labels SET ts_end = ? WHERE {where}", (ts, *params)
            )
            self.conn.execute("COMMIT")
            return cur.rowcount
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def list_labels(self, include_closed: bool = False) -> list[tuple]:
        where = "" if include_closed else " WHERE ts_end IS NULL"
        return self.conn.execute(
            "SELECT id, ts_start, ts_end, label, notes FROM labels" + where
            + " ORDER BY ts_start, id"
        ).fetchall()

    def open_labels(self) -> list[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT id, ts_start, label, notes FROM labels WHERE ts_end IS NULL"
            " ORDER BY ts_start"
        )
        return cur.fetchall()

    def count_samples(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0])

    def close(self) -> None:
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        self.conn.close()
