from __future__ import annotations

import csv
import io
import json
import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

from collector.export import export_csv
from collector.main import SCHEMA
from collector.storage import Storage


class ExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = pathlib.Path(self.tmp.name) / "metrics.db"
        self.storage = Storage(self.db, SCHEMA)
        self.addCleanup(self.storage.close)
        self.storage.ensure_columns(["temperature", "note"], {"note": "TEXT"})

    def export(self, **options):
        output = io.StringIO()
        count = export_csv(self.db, output, **options)
        rows = list(csv.DictReader(io.StringIO(output.getvalue())))
        self.assertEqual(count, len(rows))
        for row in rows:
            row["labels_json"] = json.loads(row["labels_json"])
        return rows

    def test_export_preserves_unicode_quotes_nulls_and_duplicate_timestamps(self):
        note = 'été, "chaud"\nventilation'
        self.storage.insert_samples([
            {"ts": 100, "boot_id": "a", "temperature": 42.5, "note": note},
            {"ts": 100, "boot_id": "a", "temperature": None},
        ])
        rows = self.export()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["sample_id"], rows[1]["sample_id"])
        self.assertEqual(rows[0]["note"], note)
        self.assertEqual(rows[1]["temperature"], "")
        self.assertEqual(rows[0]["labels_json"], [])

    def test_overlapping_labels_keep_one_row_per_sample_and_half_open_boundaries(self):
        self.storage.insert_samples([{"ts": ts, "boot_id": "a"} for ts in (99, 100, 150, 200)])
        first = self.storage.start_label(100, "airflow_blocked", "grille obstruée")
        self.storage.end_label(200, first)
        second = self.storage.start_label(150, "stress_ng", "load")
        rows = self.export()
        self.assertEqual([[item["id"] for item in row["labels_json"]] for row in rows],
                         [[], [first], [first, second], [second]])
        self.assertEqual(rows[1]["labels_json"], [{
            "id": first, "label": "airflow_blocked", "notes": "grille obstruée",
            "ts_start": 100, "ts_end": 200,
        }])

    def test_filters_boot_time_range_and_selected_column_order(self):
        self.storage.insert_samples([
            {"ts": 100, "boot_id": "a"}, {"ts": 150, "boot_id": "a"},
            {"ts": 150, "boot_id": "b"}, {"ts": 200, "boot_id": "a"},
        ])
        rows = self.export(boot_id="a", since=100, until=200, columns=["boot_id", "ts"])
        self.assertEqual(list(rows[0]), ["boot_id", "ts", "labels_json"])
        self.assertEqual([row["ts"] for row in rows], ["100", "150"])

    def test_invalid_columns_and_ranges_produce_no_partial_output(self):
        for options in ({"columns": ["missing"]}, {"columns": ["ts; DROP TABLE samples"]},
                        {"columns": []}, {"columns": ["ts", "ts"]},
                        {"since": 100, "until": 100}, {"since": 200, "until": 100}):
            with self.subTest(options=options):
                output = io.StringIO()
                with self.assertRaises(ValueError):
                    export_csv(self.db, output, **options)
                self.assertEqual(output.getvalue(), "")

    def test_missing_database_is_not_created(self):
        missing = self.db.parent / "missing.db"
        with self.assertRaises(sqlite3.OperationalError):
            export_csv(missing, io.StringIO())
        self.assertFalse(missing.exists())

    def test_legacy_database_is_read_without_migrating_it(self):
        legacy = self.db.parent / "legacy.db"
        with sqlite3.connect(legacy) as conn:
            conn.executescript(
                "CREATE TABLE samples (ts INTEGER PRIMARY KEY, boot_id TEXT NOT NULL);"
                "CREATE TABLE labels (id INTEGER PRIMARY KEY, ts_start INTEGER,"
                " ts_end INTEGER, label TEXT, notes TEXT);"
                "INSERT INTO samples VALUES (100, 'boot');"
            )
        conn.close()
        before = legacy.read_bytes()
        output = io.StringIO()
        self.assertEqual(export_csv(legacy, output), 1)
        self.assertEqual(legacy.read_bytes(), before)
        self.assertEqual(next(csv.DictReader(io.StringIO(output.getvalue())))["boot_id"], "boot")

    def test_sqlite_uri_metacharacters_in_the_filename_are_escaped(self):
        self.storage.insert_samples([{"ts": 1, "boot_id": "a"}])
        self.storage.close()
        special = self.db.with_name("metrics?mode=rw#test.db")
        self.db.rename(special)
        self.assertEqual(export_csv(special, io.StringIO()), 1)

    def test_concurrent_label_changes_do_not_change_the_export_snapshot(self):
        self.storage.insert_samples([{"ts": ts, "boot_id": "a"} for ts in (100, 200)])
        storage = self.storage

        class ConcurrentOutput(io.StringIO):
            changed = False

            def write(self, value):
                if not self.changed:
                    self.changed = True
                    storage.start_label(0, "added_during_export")
                return super().write(value)

        output = ConcurrentOutput()
        self.assertEqual(export_csv(self.db, output), 2)
        rows = list(csv.DictReader(io.StringIO(output.getvalue())))
        self.assertEqual([json.loads(row["labels_json"]) for row in rows], [[], []])
        self.assertEqual(len(self.storage.open_labels()), 1)

    def test_output_failure_closes_the_read_connection(self):
        connection = sqlite3.connect(self.db, isolation_level=None)
        output = mock.Mock()
        output.write.side_effect = OSError("destination full")
        with mock.patch("collector.export.sqlite3.connect", return_value=connection):
            with self.assertRaisesRegex(OSError, "destination full"):
                export_csv(self.db, output)
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_empty_export_still_has_a_header(self):
        output = io.StringIO()
        self.assertEqual(export_csv(self.db, output), 0)
        self.assertIn("sample_id,ts,boot_id,temperature,note,labels_json\n", output.getvalue())
