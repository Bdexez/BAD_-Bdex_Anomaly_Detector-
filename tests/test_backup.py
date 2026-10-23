from __future__ import annotations

import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

from collector.backup import backup_database
from collector.main import SCHEMA
from collector.storage import Storage


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = pathlib.Path(self.tmp.name) / "live.db"
        self.destination = self.db.with_name("backup.db")
        self.storage = Storage(self.db, SCHEMA)
        self.addCleanup(self.storage.close)
        self.storage.ensure_columns(["temperature"])
        self.storage.insert_samples([
            {"ts": 100, "boot_id": "a", "temperature": 42.0},
            {"ts": 100, "boot_id": "a", "temperature": 43.0},
        ])
        self.storage.start_label(50, "baseline", "été")
        self.storage.add_event(100, "kmsg", "mce", "critical", "error", "a:1")

    def test_live_wal_data_schema_labels_and_events_are_copied_into_one_file(self):
        self.assertGreater(pathlib.Path(str(self.db) + "-wal").stat().st_size, 0)
        self.assertEqual(backup_database(self.db, self.destination), self.destination)
        with sqlite3.connect(self.destination) as target:
            self.assertEqual(target.execute("SELECT ts, temperature FROM samples ORDER BY sample_id").fetchall(),
                             [(100, 42.0), (100, 43.0)])
            self.assertEqual(target.execute("SELECT label, notes FROM labels").fetchall(), [("baseline", "été")])
            self.assertEqual(target.execute("SELECT dedup FROM events").fetchall(), [("a:1",)])
            self.assertEqual(target.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(target.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        target.close()
        self.assertFalse(pathlib.Path(str(self.destination) + "-wal").exists())
        self.assertFalse(pathlib.Path(str(self.destination) + "-shm").exists())
        self.assertEqual(self.storage.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_collector_can_continue_writing_without_changing_the_backup(self):
        backup_database(self.db, self.destination)
        self.storage.insert_samples([{"ts": 200, "boot_id": "a"}])
        self.assertEqual(self.storage.count_samples(), 3)
        with sqlite3.connect(self.destination) as target:
            self.assertEqual(target.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 2)
        target.close()

    def test_existing_destination_and_source_are_never_overwritten(self):
        self.destination.write_bytes(b"existing backup")
        for destination in (self.db, self.destination):
            before = destination.read_bytes()
            with self.subTest(destination=destination), self.assertRaises(FileExistsError):
                backup_database(self.db, destination)
            self.assertEqual(destination.read_bytes(), before)

    def test_missing_source_does_not_create_a_database_or_a_backup(self):
        missing = self.db.with_name("missing.db")
        with self.assertRaises(sqlite3.OperationalError):
            backup_database(missing, self.destination)
        self.assertFalse(missing.exists())
        self.assertFalse(self.destination.exists())

    def test_timeout_cleans_the_partial_copy_and_releases_connections(self):
        with mock.patch("collector.backup.time.monotonic", side_effect=[0.0, 1.0]):
            with self.assertRaises(TimeoutError):
                backup_database(self.db, self.destination, timeout=0.1)
        self.assertFalse(self.destination.exists())
        backup_database(self.db, self.destination)
        self.assertTrue(self.destination.is_file())

    def test_invalid_timeout_is_rejected_before_creating_files(self):
        for timeout in (0, -1, float("nan"), float("inf"), -float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                backup_database(self.db, self.destination, timeout=timeout)
            self.assertFalse(self.destination.exists())

    def test_old_schema_is_copied_without_migrating_the_source(self):
        legacy = self.db.with_name("legacy.db")
        with sqlite3.connect(legacy) as conn:
            conn.executescript("CREATE TABLE samples (ts INTEGER PRIMARY KEY, boot_id TEXT);"
                               "INSERT INTO samples VALUES (1, 'a');")
        conn.close()
        before = legacy.read_bytes()
        backup_database(legacy, self.destination)
        self.assertEqual(legacy.read_bytes(), before)
        with sqlite3.connect(self.destination) as target:
            self.assertEqual(target.execute("PRAGMA table_info(samples)").fetchone()[1], "ts")
            self.assertEqual(target.execute("SELECT * FROM samples").fetchall(), [(1, "a")])
        target.close()
