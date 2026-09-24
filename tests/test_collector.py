from __future__ import annotations

from contextlib import contextmanager, redirect_stderr
import io
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from collector import main as main_module
from collector.main import SCHEMA, Collector
from collector.readers.base import Reader


class CountingReader(Reader):
    name = "counting"
    fields = ["value", "note"]
    text_fields = frozenset({"note"})

    def __init__(self):
        self.ticks = 0
        self.closed = False

    def read(self):
        self.ticks += 1
        return {"value": float(self.ticks), "note": "ok"}

    def close(self):
        self.closed = True


class BrokenReader(Reader):
    name = "broken"
    fields = ["boom"]

    def read(self):
        raise RuntimeError("capteur débranché")


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = pathlib.Path(self.tmp.name) / "metrics.db"

    def collector(self, readers):
        patcher = mock.patch.object(main_module, "candidate_readers", lambda boot="": readers)
        patcher.start()
        self.addCleanup(patcher.stop)
        return Collector(str(self.db), period=0.01, schema=SCHEMA)

    @contextmanager
    def clock(self, collector):
        """Avance aux deadlines sans attendre ni dépendre de la charge hôte."""
        now = [0.0]

        def wait(delay):
            now[0] += delay
            return False

        with mock.patch.object(main_module.time, "monotonic", side_effect=lambda: now[0]), \
             mock.patch.object(main_module, "now_ms", side_effect=lambda: 1000 + int(now[0] * 1000)), \
             mock.patch.object(collector._stop_event, "wait", side_effect=wait):
            yield now

    def query(self, sql):
        import sqlite3

        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def test_end_to_end(self):
        reader = CountingReader()
        collector = self.collector([reader])
        collector.run(duration=0.05)

        rows = self.query("SELECT ts, boot_id, value, note FROM samples ORDER BY ts")
        self.assertGreaterEqual(len(rows), 2)
        self.assertTrue(all(row[1] for row in rows))
        self.assertEqual(rows[0][2], 1.0)
        self.assertEqual(rows[0][3], "ok")
        self.assertTrue(reader.closed)

        types = {r[1]: r[2] for r in self.query("PRAGMA table_info(samples)")}
        self.assertEqual(types["value"], "REAL")
        self.assertEqual(types["note"], "TEXT")

        kinds = [k for (k,) in self.query("SELECT kind FROM events ORDER BY id")]
        self.assertEqual(kinds, ["start", "stop"])
        self.assertEqual(len(self.query("SELECT boot_id FROM boots")), 1)

    def test_broken_reader_is_disabled_and_logged_in_events(self):
        good = CountingReader()
        collector = self.collector([good, BrokenReader()])
        collector.run(duration=0.1)

        events = self.query("SELECT kind, message FROM events WHERE kind = 'reader_fail'")
        self.assertEqual(len(events), 1)
        self.assertIn("capteur débranché", events[0][1])
        # Le reader sain continue de produire malgré la panne de l'autre.
        self.assertGreaterEqual(len(self.query("SELECT value FROM samples")), 2)

    def test_write_failure_does_not_kill_the_loop(self):
        collector = self.collector([CountingReader()])
        with mock.patch.object(
            collector.storage, "insert_samples", side_effect=OSError("disque plein")
        ):
            collector.run(duration=0.05)  # ne doit pas lever

    def test_flush_is_time_based_for_slow_periods(self):
        collector = self.collector([CountingReader()])
        collector.period = 60.0
        writes = []
        insert = collector.storage.insert_samples
        with self.clock(collector) as now:
            def record(rows):
                writes.append((now[0], [row["value"] for row in rows]))
                return insert(rows)

            with mock.patch.object(collector.storage, "insert_samples", side_effect=record):
                collector.run(duration=65.0)
        # Le premier sample est écrit à 30 s, sans attendre le tick à 60 s.
        self.assertEqual(writes, [(30.0, [1.0]), (60.0, [2.0])])

    def test_duration_shorter_than_period_does_not_wait_for_next_tick(self):
        reader = CountingReader()
        collector = self.collector([reader])
        collector.period = 3600.0
        with self.clock(collector) as now:
            collector.run(duration=2.0)
            self.assertEqual(now[0], 2.0)
        self.assertEqual(reader.ticks, 1)
        self.assertTrue(reader.closed)

    def test_sigterm_wakes_a_long_wait_and_flushes_pending_samples(self):
        code = textwrap.dedent("""\
            import os
            import signal
            import sys
            import threading
            from collector import main

            main.candidate_readers = lambda boot='': []
            collector = main.Collector(sys.argv[1], period=3600.0)
            signal.signal(signal.SIGTERM, collector.stop)
            timer = threading.Timer(0.05, lambda: os.kill(os.getpid(), signal.SIGTERM))
            timer.start()
            collector.run()
            timer.join()
        """)
        result = subprocess.run(
            [sys.executable, "-c", code, str(self.db)], cwd=SCHEMA.parent,
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.query("SELECT ts FROM samples")), 1)
        self.assertEqual(self.query("SELECT kind FROM events ORDER BY id"), [("start",), ("stop",)])

    def test_slow_reader_does_not_trigger_catch_up_ticks(self):
        reader = CountingReader()
        collector = self.collector([reader])
        collector.period = 1.0
        tick = collector.tick
        ticks = []
        with self.clock(collector) as now:
            def slow_tick():
                ticks.append(now[0])
                row = tick()
                now[0] += 3.0
                return row

            with mock.patch.object(collector, "tick", side_effect=slow_tick):
                collector.run(duration=6.0)
        self.assertEqual(ticks, [0.0, 4.0])

    def test_invalid_period_is_rejected_before_creating_storage(self):
        for period in (0.0, -1.0, float("nan"), float("inf"), -float("inf")):
            with self.subTest(period=period), mock.patch.object(main_module, "Storage") as storage:
                with self.assertRaises(ValueError):
                    Collector(str(self.db), period=period)
                storage.assert_not_called()

    def test_invalid_duration_is_rejected_and_resources_are_closed(self):
        for duration in (0.0, -1.0, float("nan"), float("inf"), -float("inf")):
            with self.subTest(duration=duration):
                reader = CountingReader()
                collector = self.collector([reader])
                with self.assertRaises(ValueError):
                    collector.run(duration=duration)
                self.assertEqual(reader.ticks, 0)
                self.assertTrue(reader.closed)

    def test_cli_rejects_invalid_timings_before_starting_collection(self):
        for option in ("--period", "--duration"):
            for value in ("0", "-1", "nan", "inf", "-inf"):
                with self.subTest(option=option, value=value), \
                     mock.patch("sys.argv", ["collector", f"{option}={value}"]), \
                     mock.patch.object(main_module, "Collector") as collector, \
                     redirect_stderr(io.StringIO()) as stderr:
                    with self.assertRaises(SystemExit) as error:
                        main_module.main()
                    self.assertEqual(error.exception.code, 2)
                    self.assertIn("nombre fini > 0", stderr.getvalue())
                    collector.assert_not_called()

    def test_reader_events_reach_the_database_with_their_own_timestamp(self):
        class Alarming(CountingReader):
            def setup(self):
                # Émis pendant setup() : le rejeu du tampon noyau a lieu là,
                # donc l'émetteur doit déjà être branché.
                self.emit("mce", "erreur au démarrage", "critical", ts=111, dedup="k1")
                return True

            def read(self):
                self.emit("gpu_reset", "reset", "critical", ts=222, dedup="k2")
                return super().read()

        collector = self.collector([Alarming()])
        collector.run(duration=0.03)
        rows = self.query(
            "SELECT ts, source, kind, dedup FROM events WHERE source = 'counting' ORDER BY ts"
        )
        self.assertEqual(rows[0], (111, "counting", "mce", "k1"))
        self.assertEqual(rows[1], (222, "counting", "gpu_reset", "k2"))
        # Le tick suivant réémet la même clé : une seule ligne subsiste.
        self.assertEqual(len(rows), 2)

    def test_only_declared_fields_reach_the_database(self):
        class Chatty(CountingReader):
            def read(self):
                return {"value": 1.0, "note": "ok", "undeclared": 2.0}

        collector = self.collector([Chatty()])
        collector.run(duration=0.03)
        columns = {r[1] for r in self.query("PRAGMA table_info(samples)")}
        self.assertNotIn("undeclared", columns)

    def test_invalid_metric_does_not_lose_samples_from_other_readers(self):
        class Invalid(Reader):
            name = "invalid"
            fields = ["bad"]

            def read(self):
                return {"bad": ["not a SQLite value"]}

        collector = self.collector([CountingReader(), Invalid()])
        collector.period = 1.0
        with self.clock(collector):
            collector.run(duration=2.0)
        self.assertEqual(
            self.query("SELECT value, bad FROM samples ORDER BY ts"), [(1.0, None), (2.0, None)]
        )
