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

    def test_legacy_schema_and_unusual_column_names_are_read_without_migration(self):
        db = self.db.with_name("legacy.db")
        with sqlite3.connect(db) as conn:
            conn.executescript(
                'CREATE TABLE samples (ts INTEGER PRIMARY KEY, boot_id TEXT NOT NULL, "odd""metric" REAL);'
                "CREATE TABLE labels (id INTEGER PRIMARY KEY, ts_start INTEGER, ts_end INTEGER);"
                "CREATE TABLE events (source TEXT, kind TEXT, severity TEXT);"
                "INSERT INTO samples VALUES (1, 'boot', 0.5);"
            )
        conn.close()
        before = db.read_bytes()
        report = dataset_report(db)
        self.assertEqual(report["metrics"][0]["column"], 'odd"metric')
        self.assertEqual(report["metrics"][0]["present"], 1)
        self.assertEqual(db.read_bytes(), before)


