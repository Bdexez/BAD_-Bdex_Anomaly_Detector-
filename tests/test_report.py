from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

from collector import report as report_module
from collector.export import export_csv
from collector.main import SCHEMA
from collector.report import dataset_report
from collector.storage import Storage, quote_identifier


class DatasetReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = pathlib.Path(self.tmp.name) / "metrics.db"
        self.storage = Storage(self.db, SCHEMA)
        self.addCleanup(self.storage.close)
        self.storage.ensure_columns(["cpu_util", "note"], {"note": "TEXT"})

    def seed(self):
        self.storage.insert_samples([
            {"ts": 100, "boot_id": "a", "cpu_util": 0.0, "note": ""},
            {"ts": 100, "boot_id": "a", "note": "data"},
            {"ts": 200, "boot_id": "a", "cpu_util": 0.5},
            {"ts": 300, "boot_id": "b", "cpu_util": 1.0, "note": "été"},
        ])
        first = self.storage.start_label(100, "normal")
        self.storage.end_label(250, first)
        second = self.storage.start_label(150, "stress_ng")
        self.storage.end_label(300, second)
        self.storage.start_label(500, "future")
        self.storage.add_event(100, "kmsg", "gpu_reset", "critical")
        self.storage.add_event(200, "kmsg", "gpu_reset", "critical")
        self.storage.add_event(100, "collector", "start", "info")

    def test_coverage_counts_zero_and_empty_text_as_present(self):
        self.seed()
        report = dataset_report(self.db)
        self.assertEqual(report["samples"], {
            "count": 4, "first_ts": 100, "last_ts": 300, "labelled": 3, "labelled_pct": 75.0,
        })
        self.assertEqual(report["metrics"], [
            {"column": "cpu_util", "type": "REAL", "present": 3, "missing": 1, "coverage_pct": 75.0},
            {"column": "note", "type": "TEXT", "present": 3, "missing": 1, "coverage_pct": 75.0},
        ])
        self.assertEqual(report["labels"], {"total": 3, "open": 1})

    def test_boots_and_event_groups_are_kept_separate(self):
        self.seed()
        report = dataset_report(self.db)
        self.assertEqual(report["boots"], [
            {"boot_id": "a", "samples": 3, "first_ts": 100, "last_ts": 200, "duplicate_timestamps": 1},
            {"boot_id": "b", "samples": 1, "first_ts": 300, "last_ts": 300, "duplicate_timestamps": 0},
        ])
        self.assertEqual(report["events"], [
            {"source": "collector", "kind": "start", "severity": "info", "count": 1},
            {"source": "kmsg", "kind": "gpu_reset", "severity": "critical", "count": 2},
        ])

    def test_empty_dataset_does_not_invent_zero_percent_coverage(self):
        report = dataset_report(self.db)
        self.assertEqual(report["samples"]["count"], 0)
        self.assertIsNone(report["samples"]["labelled_pct"])
        self.assertTrue(all(metric["coverage_pct"] is None for metric in report["metrics"]))
        self.assertEqual(report["boots"], [])

    def test_time_window_scopes_every_report_section(self):
        self.seed()
        report = dataset_report(self.db, since=150, until=300)
        self.assertEqual(report["window"], {"since": 150, "until": 300})
        self.assertEqual(report["samples"], {
            "count": 1, "first_ts": 200, "last_ts": 200, "labelled": 1, "labelled_pct": 100.0,
        })
        self.assertEqual(report["boots"], [
            {"boot_id": "a", "samples": 1, "first_ts": 200, "last_ts": 200, "duplicate_timestamps": 0},
        ])
        self.assertEqual(report["metrics"], [
            {"column": "cpu_util", "type": "REAL", "present": 1, "missing": 0, "coverage_pct": 100.0},
            {"column": "note", "type": "TEXT", "present": 0, "missing": 1, "coverage_pct": 0.0},
        ])
        self.assertEqual(report["labels"], {"total": 2, "open": 0})
        self.assertEqual(report["events"], [
            {"source": "kmsg", "kind": "gpu_reset", "severity": "critical", "count": 1},
        ])

    def test_filtered_label_counts_include_only_intersecting_nonempty_windows(self):
        self.seed()
        left = self.storage.start_label(50, "ends_at_lower_bound")
        self.storage.end_label(150, left)
        self.storage.start_label(300, "starts_at_upper_bound")
        self.storage.start_label(0, "open_before_lower_bound")
        spanning = self.storage.start_label(50, "spans_range")
        self.storage.end_label(400, spanning)
        empty = self.storage.start_label(200, "empty")
        self.storage.end_label(200, empty)
        self.assertEqual(dataset_report(self.db, since=150, until=300)["labels"],
                         {"total": 4, "open": 1})

    def test_report_and_export_use_identical_inclusive_exclusive_sample_boundaries(self):
        self.seed()
        for since, until, expected in ((None, None, 4), (100, 200, 2),
                                       (300, None, 1), (None, 150, 2), (0, 1, 0)):
            with self.subTest(since=since, until=until):
                report = dataset_report(self.db, since=since, until=until)
                self.assertEqual(report["samples"]["count"], expected)
                self.assertEqual(export_csv(self.db, io.StringIO(), since=since, until=until), expected)
                self.assertEqual(sum(boot["samples"] for boot in report["boots"]), expected)
                if not expected:
                    self.assertIsNone(report["samples"]["labelled_pct"])
                    self.assertTrue(all(metric["coverage_pct"] is None for metric in report["metrics"]))

    def test_single_ended_ranges_scope_labels_and_events(self):
        self.seed()
        before = dataset_report(self.db, until=150)
        self.assertEqual(before["labels"], {"total": 1, "open": 0})
        self.assertEqual(sum(event["count"] for event in before["events"]), 2)
        after = dataset_report(self.db, since=300)
        self.assertEqual(after["labels"], {"total": 1, "open": 1})
        self.assertEqual(after["events"], [])

    def test_zero_is_a_real_boundary_for_samples_labels_and_events(self):
        self.storage.insert_samples([{"ts": -1, "boot_id": "a"}, {"ts": 0, "boot_id": "a"}])
        label = self.storage.start_label(-1, "before")
        self.storage.end_label(0, label)
        self.storage.start_label(0, "after")
        for ts in (-1, 0):
            self.storage.add_event(ts, "collector", "start", "info")
        for options, expected_ts, expected_open in (({"until": 0}, -1, 0), ({"since": 0}, 0, 1)):
            with self.subTest(options=options):
                report = dataset_report(self.db, **options)
                self.assertEqual(report["samples"]["count"], 1)
                self.assertEqual(report["samples"]["first_ts"], expected_ts)
                self.assertEqual(report["labels"], {"total": 1, "open": expected_open})
                self.assertEqual(sum(event["count"] for event in report["events"]), 1)

    def test_invalid_range_is_rejected_before_opening_the_database(self):
        for since, until in ((100, 100), (200, 100)):
            with self.subTest(since=since, until=until), \
                 mock.patch.object(report_module, "read_snapshot") as snapshot:
                with self.assertRaisesRegex(ValueError, "--until"):
                    dataset_report(self.db, since=since, until=until)
                snapshot.assert_not_called()

    def test_all_sections_use_the_same_snapshot_despite_concurrent_writes(self):
        self.seed()
        changed = False

        def concurrently_quote(name):
            nonlocal changed
            if not changed:
                changed = True
                self.storage.insert_samples([{"ts": 600, "boot_id": "a", "cpu_util": 1.0}])
                self.storage.start_label(600, "added_later")
            return quote_identifier(name)

        with mock.patch.object(report_module, "quote_identifier", side_effect=concurrently_quote):
            report = dataset_report(self.db)
        self.assertEqual(report["samples"]["count"], 4)
        self.assertEqual(report["metrics"][0]["present"], 3)
        self.assertEqual(report["labels"]["total"], 3)
        self.assertEqual(sum(boot["samples"] for boot in report["boots"]), 4)
        self.assertEqual(self.storage.count_samples(), 5)

    def test_filtered_sections_share_one_snapshot_during_concurrent_writes(self):
        self.seed()
        changed = False

        def concurrently_quote(name):
            nonlocal changed
            if not changed:
                changed = True
                self.storage.insert_samples([{"ts": 175, "boot_id": "a", "cpu_util": 1.0}])
                self.storage.start_label(100, "added_later")
                self.storage.add_event(175, "kmsg", "mce", "critical")
            return quote_identifier(name)

        with mock.patch.object(report_module, "quote_identifier", side_effect=concurrently_quote):
            report = dataset_report(self.db, since=150, until=300)
        self.assertEqual(report["samples"]["count"], 1)
        self.assertEqual(report["metrics"][0]["present"], 1)
        self.assertEqual(report["labels"], {"total": 2, "open": 0})
        self.assertEqual(sum(boot["samples"] for boot in report["boots"]), 1)
        self.assertEqual(sum(event["count"] for event in report["events"]), 1)
        self.assertEqual(self.storage.count_samples(), 5)

    def test_legacy_schema_and_unusual_column_names_are_read_without_migration(self):
        db = self.db.with_name("legacy.db")
        with sqlite3.connect(db) as conn:
            conn.executescript(
                'CREATE TABLE samples (ts INTEGER PRIMARY KEY, boot_id TEXT NOT NULL, "odd""metric" REAL);'
                "CREATE TABLE labels (id INTEGER PRIMARY KEY, ts_start INTEGER, ts_end INTEGER);"
                "CREATE TABLE events (ts INTEGER, source TEXT, kind TEXT, severity TEXT);"
                "INSERT INTO samples VALUES (1, 'boot', 0.5);"
            )
        conn.close()
        before = db.read_bytes()
        report = dataset_report(db)
        self.assertEqual(report["metrics"][0]["column"], 'odd"metric')
        self.assertEqual(report["metrics"][0]["present"], 1)
        self.assertEqual(dataset_report(db, since=1, until=2)["samples"]["count"], 1)
        self.assertEqual(dataset_report(db, since=2)["samples"]["count"], 0)
        self.assertEqual(db.read_bytes(), before)

    def test_json_and_text_cli_outputs_are_available(self):
        self.seed()
        with redirect_stdout(io.StringIO()) as output:
            report_module.main(["--db", str(self.db), "--json"])
        self.assertEqual(json.loads(output.getvalue())["samples"]["count"], 4)
        with redirect_stdout(io.StringIO()) as output:
            report_module.main(["--db", str(self.db)])
        self.assertIn("Mesures annotées : 3/4", output.getvalue())
        self.assertIn("cpu_util", output.getvalue())

    def test_missing_database_is_not_created_by_the_cli(self):
        missing = self.db.with_name("missing.db")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            report_module.main(["--db", str(missing)])
        self.assertEqual(error.exception.code, 2)
        self.assertFalse(missing.exists())


