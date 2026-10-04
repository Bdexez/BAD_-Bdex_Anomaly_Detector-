from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import pathlib
import tempfile
import unittest
from unittest import mock

from collector import label as label_module
from collector.main import SCHEMA
from collector.storage import Storage


class LabelsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = pathlib.Path(self.tmp.name) / "metrics.db"
        self.storage = Storage(self.db, SCHEMA)
        self.addCleanup(self.storage.close)

    def cli(self, *args, db=None):
        with mock.patch("sys.argv", ["label", "--db", str(db or self.db), *args]), \
             redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            label_module.main()
        return stdout.getvalue(), stderr.getvalue()

    def test_blank_labels_are_rejected_and_surrounding_space_is_removed(self):
        for value in ("", " \t\n "):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.storage.start_label(100, value)
        self.storage.start_label(100, " normal ")
        self.assertEqual(self.storage.open_labels()[0][2], "normal")

    def test_end_all_is_atomic_when_one_window_starts_after_the_end(self):
        self.storage.start_label(100, "normal")
        self.storage.start_label(200, "gaming")
        with self.assertRaisesRegex(ValueError, "fin précède"):
            self.storage.end_label(150)
        self.assertEqual(len(self.storage.open_labels()), 2)
        self.assertFalse(self.storage.conn.in_transaction)
        self.assertEqual(self.storage.end_label(300), 2)

    def test_ending_a_selected_window_does_not_check_unrelated_windows(self):
        first = self.storage.start_label(100, "normal")
        second = self.storage.start_label(200, "gaming")
        self.assertEqual(self.storage.end_label(150, first), 1)
        self.assertEqual(self.storage.open_labels()[0][0], second)

    def test_invalid_label_id_cannot_close_all_windows(self):
        self.storage.start_label(100, "normal")
        for label_id in (0, -1):
            with self.subTest(label_id=label_id), self.assertRaises(ValueError):
                self.storage.end_label(200, label_id)
        self.assertEqual(len(self.storage.open_labels()), 1)

    def test_history_includes_notes_and_both_boundaries(self):
        first = self.storage.start_label(100, "normal", "baseline")
        second = self.storage.start_label(200, "gaming")
        self.storage.end_label(200, first)
        self.assertEqual(self.storage.list_labels(), [(second, 200, None, "gaming", "")])
        self.assertEqual(self.storage.list_labels(include_closed=True), [
            (first, 100, 200, "normal", "baseline"), (second, 200, None, "gaming", ""),
        ])

    def test_cli_can_show_closed_labels_without_hiding_open_ones(self):
        first = self.storage.start_label(100, "airflow_blocked", "intake")
        self.storage.end_label(200, first)
        self.storage.start_label(300, "gaming")
        output, _ = self.cli("list")
        self.assertNotIn("airflow_blocked", output)
        output, _ = self.cli("list", "--all")
        self.assertIn("airflow_blocked", output)
        self.assertIn("intake", output)
        self.assertIn("gaming", output)
        self.assertIn("en cours", output)

    def test_cli_does_not_create_a_missing_database_when_listing_or_ending(self):
        missing = self.db.parent / "typo.db"
        for command in ("list", "end"):
            with self.subTest(command=command), self.assertRaises(SystemExit) as error:
                self.cli(command, db=missing)
            self.assertEqual(error.exception.code, 2)
            self.assertFalse(missing.exists())

    def test_cli_rejects_blank_labels_before_creating_a_database(self):
        missing = self.db.parent / "blank.db"
        with self.assertRaises(SystemExit) as error:
            self.cli("start", " \t ", db=missing)
        self.assertEqual(error.exception.code, 2)
        self.assertFalse(missing.exists())

    def test_cli_reports_invalid_end_without_closing_the_window(self):
        self.storage.start_label(100, "normal")
        with mock.patch.object(label_module, "now_ms", return_value=50):
            with self.assertRaises(SystemExit) as error:
                self.cli("end")
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(len(self.storage.open_labels()), 1)
