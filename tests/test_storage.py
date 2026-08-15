from __future__ import annotations

import pathlib
import sqlite3
import tempfile
import unittest

from collector.storage import Storage, check_identifier

SCHEMA = pathlib.Path(__file__).resolve().parent.parent / "schema.sql"


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = pathlib.Path(self.tmp.name) / "sub" / "metrics.db"
        self.storage = Storage(self.db, SCHEMA)
        self.addCleanup(self.storage.close)

    def test_creates_parent_directory(self):
        self.assertTrue(self.db.exists())

    def test_ensure_columns_is_idempotent_and_typed(self):
        self.storage.ensure_columns(["k10temp_tctl", "active_window"], {"active_window": "TEXT"})
        self.storage.ensure_columns(["k10temp_tctl"])
        types = {
            r[1]: r[2] for r in self.storage.conn.execute("PRAGMA table_info(samples)")
        }
        self.assertEqual(types["k10temp_tctl"], "REAL")
        self.assertEqual(types["active_window"], "TEXT")

    def test_rejects_hostile_identifiers(self):
        for bad in ("1temp", "temp; DROP TABLE samples", "Temp", "", "a" * 80):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                check_identifier(bad)

    def test_rejects_unknown_column_type(self):
        with self.assertRaises(ValueError):
            self.storage.ensure_columns(["x"], {"x": "REAL); DROP TABLE samples --"})

    def test_insert_heterogeneous_rows(self):
        self.storage.ensure_columns(["a", "b"])
        written = self.storage.insert_samples(
            [{"ts": 1, "boot_id": "z", "a": 1.0}, {"ts": 2, "boot_id": "z", "b": 2.0}]
        )
        self.assertEqual(written, 2)
        rows = self.storage.conn.execute("SELECT ts, a, b FROM samples ORDER BY ts").fetchall()
        self.assertEqual(rows, [(1, 1.0, None), (2, None, 2.0)])

    def test_unknown_columns_do_not_lose_the_batch(self):
        self.storage.ensure_columns(["a"])
        written = self.storage.insert_samples([{"ts": 1, "boot_id": "z", "a": 1.0, "surprise": 9}])
        self.assertEqual(written, 1)
        self.assertEqual(
            self.storage.conn.execute("SELECT a FROM samples").fetchone(), (1.0,)
        )

    def test_labels_lifecycle(self):
        first = self.storage.start_label(100, "stress_ng", "essai")
        self.storage.start_label(150, "gaming")
        self.assertEqual(len(self.storage.open_labels()), 2)
        self.assertEqual(self.storage.end_label(200, first), 1)
        self.assertEqual(self.storage.end_label(200, first), 0)  # déjà fermé
        self.assertEqual(self.storage.end_label(300), 1)  # ferme le reste
        self.assertEqual(self.storage.open_labels(), [])

    def test_reopening_existing_db_keeps_columns(self):
        self.storage.ensure_columns(["a"])
        self.storage.insert_samples([{"ts": 1, "boot_id": "z", "a": 1.0}])
        self.storage.close()
        again = Storage(self.db, SCHEMA)
        self.addCleanup(again.close)
        self.assertIn("a", again._columns)
        self.assertEqual(again.count_samples(), 1)

    def test_empty_batch_is_a_noop(self):
        self.assertEqual(self.storage.insert_samples([]), 0)

    def test_event_dedup_makes_replay_idempotent(self):
        # Rejouer le tampon noyau à chaque démarrage ne doit pas créer trois
        # fois la même MCE.
        self.assertTrue(self.storage.add_event(1, "kmsg", "mce", "error", "boum", "boot:42"))
        self.assertFalse(self.storage.add_event(1, "kmsg", "mce", "error", "boum", "boot:42"))
        self.assertEqual(self.storage.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)

    def test_events_without_dedup_key_are_never_merged(self):
        for _ in range(3):
            self.assertTrue(self.storage.add_event(1, "collector", "start", "info", ""))
        self.assertEqual(self.storage.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 3)

    def test_long_messages_are_truncated(self):
        self.storage.add_event(1, "kmsg", "oops", "critical", "x" * 9000)
        (message,) = self.storage.conn.execute("SELECT message FROM events").fetchone()
        self.assertEqual(len(message), 4000)

    def test_migrates_a_database_created_by_an_older_schema(self):
        old_schema = pathlib.Path(self.tmp.name) / "vieux.sql"
        old_schema.write_text(
            "CREATE TABLE IF NOT EXISTS samples (ts INTEGER PRIMARY KEY, boot_id TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS boots (boot_id TEXT PRIMARY KEY, ts_first INTEGER,"
            " kernel TEXT, notes TEXT);"
            "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " ts INTEGER NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL,"
            " severity TEXT, message TEXT);"
            "CREATE TABLE IF NOT EXISTS labels (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " ts_start INTEGER NOT NULL, ts_end INTEGER, label TEXT NOT NULL, notes TEXT);"
        )
        legacy = pathlib.Path(self.tmp.name) / "legacy.db"
        first = Storage(legacy, old_schema)
        first.add_event(1, "collector", "start", "info", "")
        first.close()

        # Ouverte par la version courante : la colonne manquante est ajoutée
        # sans perdre les 14 jours déjà collectés.
        migrated = Storage(legacy, SCHEMA)
        self.addCleanup(migrated.close)
        columns = {r[1] for r in migrated.conn.execute("PRAGMA table_info(events)")}
        self.assertIn("dedup", columns)
        self.assertTrue(migrated.add_event(2, "kmsg", "mce", "error", "x", "k"))
        self.assertFalse(migrated.add_event(2, "kmsg", "mce", "error", "x", "k"))
        self.assertEqual(migrated.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 2)

    def test_events(self):
        self.storage.add_event(1, "collector", "reader_fail", "warning", "nvml: boom")
        row = self.storage.conn.execute("SELECT source, kind, message FROM events").fetchone()
        self.assertEqual(row, ("collector", "reader_fail", "nvml: boom"))


if __name__ == "__main__":
    unittest.main()
