from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

from collector import backup as backup_module
from collector.main import SCHEMA
from collector.storage import Storage


class BackupCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = pathlib.Path(self.tmp.name) / "metrics.db"
        self.output = self.db.with_name("snapshot.db")
        self.storage = Storage(self.db, SCHEMA)
        self.addCleanup(self.storage.close)
        self.storage.insert_samples([{"ts": 1, "boot_id": "a"}])

    def cli(self, *args):
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            backup_module.main(["--db", str(self.db), "--output", str(self.output), *args])
        return out.getvalue()

    def test_cli_produces_a_readable_backup(self):
        self.assertIn(str(self.output), self.cli())
        with sqlite3.connect(self.output) as conn:
            self.assertEqual(conn.execute("SELECT ts, boot_id FROM samples").fetchall(), [(1, "a")])
        conn.close()

    def test_cli_requires_an_explicit_destination(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            backup_module.main(["--db", str(self.db)])
        self.assertEqual(error.exception.code, 2)

    def test_invalid_timeout_has_a_clean_error_and_creates_no_file(self):
        for value in ("0", "-1", "nan", "inf", "-inf"):
            with self.subTest(value=value), self.assertRaises(SystemExit) as error:
                self.cli(f"--timeout={value}")
            self.assertEqual(error.exception.code, 2)
            self.assertFalse(self.output.exists())

    def test_cli_refuses_to_overwrite_a_backup(self):
        self.output.write_bytes(b"existing snapshot")
        with self.assertRaises(SystemExit) as error:
            self.cli()
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(self.output.read_bytes(), b"existing snapshot")

    def test_cli_timeout_cleans_up_the_partial_file(self):
        with mock.patch("collector.backup.time.monotonic", side_effect=[0.0, 1.0]):
            with self.assertRaises(SystemExit) as error:
                self.cli("--timeout=0.1")
        self.assertEqual(error.exception.code, 2)
        self.assertFalse(self.output.exists())

    def test_keyboard_interrupt_removes_the_partial_copy(self):
        with mock.patch("collector.backup.time.monotonic", side_effect=[0.0, KeyboardInterrupt()]):
            with self.assertRaises(KeyboardInterrupt):
                self.cli()
        self.assertFalse(self.output.exists())
