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







if __name__ == "__main__":
    unittest.main()
