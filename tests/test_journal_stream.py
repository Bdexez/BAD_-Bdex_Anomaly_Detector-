from __future__ import annotations

import io
import json
import subprocess
import sys
import unittest
from unittest import mock

from collector.readers import kmsg
from collector.readers.kmsg import _JournalBackend


def line(cursor="a"):
    return json.dumps({"MESSAGE": "Kernel panic", "__REALTIME_TIMESTAMP": "1000",
                       "PRIORITY": "3", "__CURSOR": cursor}).encode()


class JournalStreamTest(unittest.TestCase):
    def backend(self, reads, status=None):
        proc = mock.Mock()
        proc.poll.return_value = status
        proc.returncode = status
        proc.stdout.read.side_effect = reads
        with mock.patch.object(kmsg.subprocess, "Popen", return_value=proc), \
             mock.patch.object(kmsg.os, "set_blocking"):
            backend = _JournalBackend()
        self.addCleanup(backend.close)
        return backend, proc

    def test_continuous_input_has_a_byte_budget(self):
        backend, proc = self.backend(lambda size: b"x" * size)
        with mock.patch.object(kmsg, "MAX_JOURNAL_READ_BYTES", 128):
            self.assertEqual(list(backend.records(10)), [])
        self.assertEqual(proc.stdout.read.call_args_list, [mock.call(128)])

    def test_invalid_lines_also_consume_the_record_budget(self):
        stream = io.BytesIO(b"[]\n" * 10 + line() + b"\n")
        backend, _ = self.backend(stream.read)
        with mock.patch.object(backend, "_parse", wraps=backend._parse) as parse:
            self.assertEqual(list(backend.records(3)), [])
        self.assertEqual(parse.call_count, 3)
        self.assertEqual([r.dedup for r in backend.records(10)], ["a"])

    def test_fragments_are_reassembled_across_ticks(self):
        payload = line()
        backend, _ = self.backend([payload[:10], None, payload[10:] + b"\n", None])
        self.assertEqual(list(backend.records(10)), [])
        self.assertEqual([r.text for r in backend.records(10)], ["Kernel panic"])

    def test_final_buffered_records_are_delivered_before_process_failure(self):
        backend, _ = self.backend([line("a") + b"\n" + line("b") + b"\n", b""], status=0)
        self.assertEqual([r.dedup for r in backend.records(1)], ["a"])
        self.assertEqual([r.dedup for r in backend.records(1)], ["b"])
        with self.assertRaisesRegex(OSError, "journalctl s'est arrêté"):
            list(backend.records(1))

    def test_valid_final_line_does_not_require_a_newline(self):
        backend, _ = self.backend([line(), b""], status=0)
        self.assertEqual([r.dedup for r in backend.records(10)], ["a"])

    def test_overflow_discards_old_data_at_a_line_boundary(self):
        payload = b"\n".join(line(str(i)) for i in range(10)) + b"\n"
        backend, _ = self.backend([payload, None])
        with mock.patch.object(kmsg, "MAX_JOURNAL_BUFFER_BYTES", len(line()) * 3), \
             mock.patch.object(kmsg.log, "warning") as warning:
            records = list(backend.records(20))
        self.assertEqual(records[-1].dedup, "9")
        self.assertLess(len(records), 10)
        warning.assert_called_once()

    def test_oversized_partial_line_is_discarded_until_the_next_separator(self):
        backend, _ = self.backend([b"x" * 100, None, b"tail\n" + line() + b"\n", None])
        with mock.patch.object(kmsg, "MAX_JOURNAL_BUFFER_BYTES", 90):
            self.assertEqual(list(backend.records(10)), [])
        self.assertEqual([r.dedup for r in backend.records(10)], ["a"])

    def test_forced_shutdown_waits_for_the_child_and_is_idempotent(self):
        backend, proc = self.backend([])
        proc.wait.side_effect = [subprocess.TimeoutExpired("journalctl", 2), 0]
        backend.close()
        backend.close()
        proc.kill.assert_called_once()
        self.assertEqual(proc.wait.call_count, 2)
        proc.stdout.close.assert_called_once()

    def test_failed_initialization_closes_the_child(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        with mock.patch.object(kmsg.subprocess, "Popen", return_value=proc), \
             mock.patch.object(kmsg.os, "set_blocking", side_effect=OSError("nonblocking failed")):
            with self.assertRaises(OSError):
                _JournalBackend()
        proc.terminate.assert_called_once()
        proc.wait.assert_called_once()
        proc.stdout.close.assert_called_once()


class JournalReplayTest(unittest.TestCase):
    def replay(self, script):
        backend = _JournalBackend.__new__(_JournalBackend)
        backend.REPLAY = (sys.executable, "-u", "-c", script)
        return list(backend.backfill())

    def test_replay_streams_a_real_process_to_eof(self):
        payload = line("a") + b"\n" + line("b")
        records = self.replay(f"import sys; sys.stdout.buffer.write({payload!r})")
        self.assertEqual([record.dedup for record in records], ["a", "b"])

    def test_replay_stops_a_continuous_producer_at_the_record_limit(self):
        payload = line() + b"\n"
        script = f"import sys\nwhile True:\n sys.stdout.buffer.write({payload!r} * 1000)\n sys.stdout.flush()"
        with mock.patch.object(kmsg, "MAX_BACKFILL_RECORDS", 3):
            self.assertEqual(len(self.replay(script)), 3)

    def test_replay_timeout_terminates_an_idle_process(self):
        with mock.patch.object(kmsg, "BACKFILL_TIMEOUT", 0.02), \
             mock.patch.object(kmsg.log, "warning") as warning:
            self.assertEqual(self.replay("import time; time.sleep(1)"), [])
        warning.assert_called_once()

    def test_replay_failure_keeps_records_already_received(self):
        payload = line() + b"\n"
        with mock.patch.object(kmsg.log, "warning") as warning:
            records = self.replay(f"import sys; sys.stdout.buffer.write({payload!r}); sys.exit(1)")
        self.assertEqual([record.dedup for record in records], ["a"])
        self.assertGreaterEqual(warning.call_count, 1)
