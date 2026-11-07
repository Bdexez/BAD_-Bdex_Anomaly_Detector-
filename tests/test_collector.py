from __future__ import annotations

from contextlib import contextmanager, redirect_stderr
import io
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from collector import main as main_module
from collector.main import SCHEMA, Collector
from collector.readers.base import Reader
from collector.storage import Storage


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
        collector = Collector(str(self.db), period=0.01, schema=SCHEMA)
        self.addCleanup(collector.close)
        return collector

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

    def test_equal_wall_clock_timestamps_do_not_overwrite_ticks(self):
        reader = CountingReader()
        collector = self.collector([reader])
        collector.period = 1.0
        with self.clock(collector), mock.patch.object(main_module, "now_ms", return_value=1000):
            collector.run(duration=3.0)
        self.assertEqual(
            self.query("SELECT ts, value FROM samples ORDER BY sample_id"),
            [(1000, 1.0), (1000, 2.0), (1000, 3.0)],
        )

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

    def test_samples_survive_a_transient_write_failure(self):
        collector = self.collector([])
        buffer = [{"ts": 1, "boot_id": "b"}, {"ts": 2, "boot_id": "b"}]
        expected = buffer.copy()
        with mock.patch.object(collector.storage, "insert_samples", side_effect=OSError("plein")):
            collector.flush(buffer)
        self.assertEqual(buffer, expected)
        self.assertEqual(collector.storage.count_samples(), 0)
        collector.flush(buffer)
        self.assertEqual(buffer, [])
        self.assertEqual(self.query("SELECT ts FROM samples ORDER BY ts"), [(1,), (2,)])

    def test_interrupted_flush_retries_pending_samples_during_shutdown(self):
        class InterruptedConnection(sqlite3.Connection):
            interrupt = True

            def executemany(self, sql, parameters):
                if self.interrupt:
                    self.interrupt = False
                    super().execute(sql, parameters[0])
                    raise KeyboardInterrupt
                return super().executemany(sql, parameters)

        reader = CountingReader()
        collector = self.collector([reader])
        collector.storage.conn.close()
        collector.storage.conn = sqlite3.connect(self.db, isolation_level=None, factory=InterruptedConnection)
        with self.clock(collector), mock.patch.object(main_module, "FLUSH_EVERY", 1):
            collector.run(duration=1.0)
        self.assertEqual(self.query("SELECT value FROM samples"), [(1.0,)])
        self.assertEqual(self.query("SELECT kind FROM events ORDER BY id"), [("start",), ("stop",)])
        self.assertTrue(reader.closed)

    def test_persistent_failure_keeps_a_bounded_buffer_and_retries_in_batches(self):
        collector = self.collector([CountingReader()])
        collector.period = 1.0
        insert = collector.storage.insert_samples
        attempts = []

        def unavailable_then_recovered(rows):
            attempts.append(len(rows))
            if len(attempts) <= 3:
                raise sqlite3.OperationalError("disque plein")
            return insert(rows)

        with self.clock(collector), \
             mock.patch.object(main_module, "MAX_BUFFER", 3), \
             mock.patch.object(main_module, "FLUSH_EVERY", 2), \
             mock.patch.object(collector.storage, "insert_samples", side_effect=unavailable_then_recovered):
            collector.run(duration=5.5)
        self.assertEqual(attempts, [2, 3, 3, 3])
        self.assertEqual(self.query("SELECT value FROM samples ORDER BY ts"), [(4.0,), (5.0,), (6.0,)])

    def test_locked_database_does_not_disable_an_event_emitting_reader(self):
        class Alarming(CountingReader):
            def read(self):
                self.emit("mce", "erreur", "error", ts=0, dedup="boot:1")
                return super().read()

        reader = Alarming()
        collector = self.collector([reader])
        collector.storage.conn.execute("PRAGMA busy_timeout=0")
        blocker = sqlite3.connect(self.db)
        self.addCleanup(blocker.close)
        blocker.execute("BEGIN IMMEDIATE")
        for _ in range(4):
            collector.tick()
        collector.flush([])  # Échec de rejeu : la file doit rester intacte.
        self.assertTrue(collector.readers[0].enabled)
        self.assertEqual(collector.readers[0].failures, 0)
        self.assertEqual(len(collector._event_buffer), 1)
        blocker.rollback()
        collector.flush([])
        self.assertEqual(len(collector._event_buffer), 0)
        self.assertEqual(collector._event_dedup, set())
        self.assertEqual(
            self.query("SELECT ts, dedup FROM events WHERE kind = 'mce'"), [(0, "boot:1")]
        )

    def test_event_queue_is_bounded_and_deduplicated(self):
        collector = self.collector([])
        with mock.patch.object(main_module, "MAX_EVENT_BUFFER", 2), \
             mock.patch.object(collector.storage, "add_event", side_effect=sqlite3.OperationalError("plein")):
            for key in ("a", "b"):
                self.assertTrue(collector._store_event("test", "fault", "warning", "x" * 5000, 1, key))
            self.assertFalse(collector._store_event("test", "fault", "warning", "x", 1, "b"))
            self.assertTrue(collector._store_event("test", "fault", "warning", "x", 2, "c"))
        self.assertEqual(len(collector._event_buffer), 2)
        self.assertEqual(collector._event_dedup, {"b", "c"})
        collector.flush([])
        self.assertEqual(
            self.query("SELECT dedup, length(message) FROM events WHERE source = 'test' ORDER BY id"),
            [("b", 4000), ("c", 1)],
        )
        self.assertTrue(collector._store_event("test", "fault", "warning", "retry", 1, "a"))

    def test_shutdown_closes_resources_even_when_database_is_unavailable(self):
        reader = CountingReader()
        collector = self.collector([reader])
        with mock.patch.object(collector.storage, "add_event", side_effect=sqlite3.OperationalError("plein")), \
             mock.patch.object(collector.storage, "insert_samples", side_effect=OSError("plein")), \
             mock.patch.object(collector.storage, "count_samples", side_effect=sqlite3.OperationalError("erreur")):
            collector.close([{"ts": 1, "boot_id": "b"}])
        self.assertTrue(reader.closed)
        with self.assertRaises(sqlite3.ProgrammingError):
            collector.storage.conn.execute("SELECT 1")
        collector.close()  # Un second close est sans effet.

    def test_unavailable_and_failed_setup_readers_are_closed(self):
        absent, broken, good = CountingReader(), CountingReader(), CountingReader()
        absent.setup = mock.Mock(return_value=False)
        broken.setup = mock.Mock(side_effect=RuntimeError("configuration impossible"))
        collector = self.collector([absent, broken, good])
        self.assertTrue(absent.closed)
        self.assertTrue(broken.closed)
        self.assertEqual([state.reader for state in collector.readers], [good])

    def test_close_failure_does_not_prevent_other_readers_from_closing(self):
        broken, good = CountingReader(), CountingReader()
        broken.close = mock.Mock(side_effect=OSError("fermeture impossible"))
        collector = self.collector([broken, good])
        collector.close()
        self.assertTrue(good.closed)
        with self.assertRaises(sqlite3.ProgrammingError):
            collector.storage.conn.execute("SELECT 1")

    def test_doctor_closes_a_reader_after_setup_failure(self):
        reader = CountingReader()
        reader.setup = mock.Mock(side_effect=RuntimeError("configuration impossible"))
        with mock.patch.object(main_module, "candidate_readers", return_value=[reader]), \
             mock.patch("builtins.print"):
            main_module.run_doctor()
        self.assertTrue(reader.closed)

    def test_scan_closes_a_reader_after_setup_failure(self):
        reader, counters = CountingReader(), CountingReader()
        reader.setup = mock.Mock(side_effect=RuntimeError("configuration impossible"))
        with mock.patch.object(main_module.R, "KmsgReader", return_value=reader), \
             mock.patch.object(main_module.R, "ErrorCounterReader", return_value=counters), \
             mock.patch("builtins.print"):
            main_module.run_scan()
        self.assertTrue(reader.closed)
        self.assertTrue(counters.closed)

    def test_failed_initialization_closes_active_readers_and_storage(self):
        reader = CountingReader()
        storage = Storage(self.db, SCHEMA)
        self.addCleanup(storage.close)
        with mock.patch.object(main_module, "Storage", return_value=storage), \
             mock.patch.object(storage, "ensure_columns", side_effect=sqlite3.OperationalError("plein")), \
             mock.patch.object(main_module, "candidate_readers", return_value=[reader]):
            with self.assertRaises(sqlite3.OperationalError):
                Collector(str(self.db), period=1.0)
        self.assertTrue(reader.closed)
        with self.assertRaises(sqlite3.ProgrammingError):
            storage.conn.execute("SELECT 1")

    def test_invalid_reader_schema_is_isolated_from_healthy_readers(self):
        for fields in (["invalid column"], ["ts"], ["boot_id"], ["sample_id"],
                       ["bad", "bad"], "bad"):
            with self.subTest(fields=fields):
                bad, good = CountingReader(), CountingReader()
                bad.fields = fields
                bad.text_fields = frozenset()
                collector = self.collector([bad, good])
                self.assertTrue(bad.closed)
                self.assertEqual([s.reader for s in collector.readers], [good])
                collector.flush([collector.tick()])
                self.assertEqual(self.query("SELECT value, note FROM samples ORDER BY sample_id DESC LIMIT 1"),
                                 [(1.0, "ok")])
                collector.close()

    def test_conflicting_reader_is_closed_without_overwriting_metrics(self):
        first, conflict = CountingReader(), CountingReader()
        conflict.name = "conflict"
        conflict.read = lambda: {"value": 999.0, "note": "overwritten"}
        collector = self.collector([first, conflict])
        self.assertTrue(conflict.closed)
        self.assertFalse(first.closed)
        collector.flush([collector.tick()])
        self.assertEqual(self.query("SELECT value, note FROM samples"), [(1.0, "ok")])

    def test_interrupted_setup_closes_current_and_previously_active_readers(self):
        first, interrupted = CountingReader(), CountingReader()
        interrupted.setup = mock.Mock(side_effect=KeyboardInterrupt)
        storage = Storage(self.db, SCHEMA)
        self.addCleanup(storage.close)
        with mock.patch.object(main_module, "Storage", return_value=storage), \
             mock.patch.object(main_module, "candidate_readers", return_value=[first, interrupted]):
            with self.assertRaises(KeyboardInterrupt):
                Collector(str(self.db), period=1.0)
        self.assertTrue(first.closed)
        self.assertTrue(interrupted.closed)
        with self.assertRaises(sqlite3.ProgrammingError):
            storage.conn.execute("SELECT 1")

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

    def test_unencodable_text_does_not_poison_a_sqlite_batch(self):
        class InvalidText(Reader):
            name = "invalid_text"
            fields = ["bad_text"]
            text_fields = frozenset({"bad_text"})

            def read(self):
                return {"bad_text": "bad\udcff"}

        collector = self.collector([CountingReader(), InvalidText()])
        collector.flush([collector.tick(), collector.tick()])
        self.assertEqual(self.query("SELECT value, note, bad_text FROM samples ORDER BY sample_id"),
                         [(1.0, "ok", None), (2.0, "ok", None)])
