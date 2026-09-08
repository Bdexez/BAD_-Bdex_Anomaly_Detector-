from __future__ import annotations

import json
import unittest

from collector import errors
from collector.readers import kmsg
from collector.readers.kmsg import KmsgReader, Record, _JournalBackend, _KmsgBackend


class FakeBackend:
    name = "fake"

    def __init__(self, history=(), live=()):
        self.history = list(history)
        self.live = list(live)
        self.closed = False

    def backfill(self, *_):
        yield from self.history

    def records(self, limit):
        batch, self.live = self.live[:limit], self.live[limit:]
        yield from batch

    def close(self):
        self.closed = True


def record(text, level=3, seq="1", ts=1000):
    return Record(ts_ms=ts, level=level, text=text, dedup=f"boot:{seq}")


class ReaderTest(unittest.TestCase):
    def build(self, history=(), live=()):
        self.emitted = []
        reader = KmsgReader(boot="boot", backend=FakeBackend(history, live))
        reader.emitter = lambda *args: (self.emitted.append(args), True)[1]
        return reader

    def test_backfill_replays_the_existing_buffer(self):
        # Les erreurs d'avant le lancement du collector comptent autant que les
        # suivantes : au boot, ou pendant que le service était arrêté.
        reader = self.build(history=[record("Kernel panic - not syncing")])
        self.assertTrue(reader.setup())
        self.assertEqual(reader.backfilled, 1)
        self.assertEqual(self.emitted[0][1], "panic")

    def test_event_keeps_its_own_timestamp_not_the_tick(self):
        reader = self.build(history=[record("Kernel panic", ts=1234567)])
        reader.setup()
        source, kind, severity, message, ts, dedup = self.emitted[0]
        self.assertEqual(ts, 1234567)
        self.assertEqual(dedup, "boot:1")
        self.assertEqual(severity, errors.CRITICAL)

    def test_counts_are_features(self):
        reader = self.build(live=[
            record("Kernel panic - not syncing"),
            record("nvme nvme0: I/O 3 QID 1 timeout", seq="2"),
            record("usb 1-1: new device", level=6, seq="3"),
        ])
        reader.setup()
        counts = reader.read()
        self.assertEqual(counts["kmsg_criticals"], 1.0)
        self.assertEqual(counts["kmsg_errors"], 1.0)
        self.assertEqual(counts["kmsg_messages"], 3.0)

    def test_quiet_tick_is_zero_not_none(self):
        reader = self.build()
        reader.setup()
        self.assertEqual(reader.read()["kmsg_errors"], 0.0)

    def test_storm_is_capped_but_reported(self):
        # Une boucle de reset GPU ne doit pas bloquer la boucle de collecte,
        # mais la tempête reste un fait à enregistrer.
        flood = [record("amdgpu: GPU reset begin!", seq=str(i)) for i in range(120)]
        reader = self.build(live=flood)
        reader.setup()
        counts = reader.read()
        self.assertEqual(counts["kmsg_criticals"], 120.0)
        kinds = [e[1] for e in self.emitted]
        self.assertEqual(kinds.count("gpu_reset"), kmsg.MAX_EVENTS_PER_TICK)
        self.assertEqual(kinds.count("event_storm"), 1)

    def test_xid_is_translated_in_the_message(self):
        reader = self.build(live=[record("NVRM: Xid (PCI:0000:01:00): 79, blah")])
        reader.setup()
        reader.read()
        self.assertIn("tombé du bus", self.emitted[0][3])

    def test_duplicate_refused_by_storage_does_not_consume_the_budget(self):
        # L'émetteur renvoie False : l'événement existait déjà. Le rejeu d'un
        # tampon déjà connu ne doit pas être compté comme une tempête.
        reader = KmsgReader(boot="b", backend=FakeBackend(
            live=[record("Kernel panic", seq=str(i)) for i in range(80)]
        ))
        seen = []
        reader.emitter = lambda *args: (seen.append(args), False)[1]
        reader.setup()
        reader.read()
        self.assertEqual(len(seen), 80)
        self.assertNotIn("event_storm", [e[1] for e in seen])

    def test_close_releases_the_backend(self):
        reader = self.build()
        reader.setup()
        backend = reader.backend
        reader.close()
        self.assertTrue(backend.closed)


class KmsgParsingTest(unittest.TestCase):
    def parse(self, raw):
        backend = _KmsgBackend.__new__(_KmsgBackend)
        backend.origin = 0.0
        backend.boot = "b"
        return backend._parse(raw)

    def test_parses_priority_sequence_and_timestamp(self):
        rec = self.parse("6,1234,5000000,-;usb 1-1: nouveau périphérique\n SUBSYSTEM=usb")
        self.assertEqual(rec.level, 6)
        self.assertEqual(rec.dedup, "b:1234")
        self.assertEqual(rec.ts_ms, 5000)  # 5 000 000 µs
        self.assertEqual(rec.text, "usb 1-1: nouveau périphérique")

    def test_level_is_extracted_from_the_facility(self):
        # priorité = facilité * 8 + niveau ; 27 = facilité 3, niveau 3 (err)
        self.assertEqual(self.parse("27,1,0,-;erreur").level, 3)

    def test_malformed_records_are_dropped(self):
        for raw in ("pas de point-virgule", "a,b,c;texte", "6;texte"):
            with self.subTest(raw=raw):
                self.assertIsNone(self.parse(raw))


class JournalParsingTest(unittest.TestCase):
    def test_parses_json_entry(self):
        line = json.dumps({
            "__CURSOR": "s=abc;i=1",
            "__REALTIME_TIMESTAMP": "1700000000123456",
            "PRIORITY": "3",
            "MESSAGE": "EXT4-fs error (device sda1)",
        }).encode()
        rec = _JournalBackend._parse(line)
        self.assertEqual(rec.ts_ms, 1700000000123)
        self.assertEqual(rec.level, 3)
        self.assertEqual(rec.dedup, "s=abc;i=1")

    def test_binary_message_encoded_as_byte_list(self):
        line = json.dumps({
            "__CURSOR": "c", "__REALTIME_TIMESTAMP": "1000000",
            "PRIORITY": "3", "MESSAGE": list(b"panic"),
        }).encode()
        self.assertEqual(_JournalBackend._parse(line).text, "panic")

    def test_garbage_lines_are_dropped(self):
        self.assertIsNone(_JournalBackend._parse(b"pas du json"))
        self.assertIsNone(_JournalBackend._parse(b"{}"))
