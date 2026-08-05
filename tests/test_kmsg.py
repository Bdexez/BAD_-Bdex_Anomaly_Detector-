from __future__ import annotations

import json
import unittest

from collector import errors
from collector.readers import kmsg
from collector.readers.kmsg import Record, _KmsgBackend








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


