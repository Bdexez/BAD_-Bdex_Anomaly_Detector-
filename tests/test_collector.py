from __future__ import annotations

import pathlib
import tempfile
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
        # 3 samples seulement : bien en dessous de FLUSH_EVERY. Sans flush au
        # temps, rien n'atteindrait le disque avant l'arrêt.
        collector = self.collector([CountingReader()])
        with mock.patch.object(main_module, "FLUSH_INTERVAL", 0.0):
            collector.run(duration=0.03)
        self.assertGreaterEqual(len(self.query("SELECT ts FROM samples")), 2)

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
