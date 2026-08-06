from __future__ import annotations

import json
import unittest

from collector import errors
from collector.readers import kmsg
from collector.readers.kmsg import Record, _JournalBackend, _KmsgBackend








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
