from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest import mock

from collector.readers.context import ContextReader


class ContextIpcTest(unittest.TestCase):
    def query(self, chunks, clock=None):
        reader = ContextReader()
        sock = mock.MagicMock()
        sock.__enter__.return_value = sock
        sock.recv.side_effect = chunks
        with mock.patch.object(reader, "_hypr_sockets", return_value=[pathlib.Path("/socket")]), \
             mock.patch("collector.readers.context.socket.socket", return_value=sock), \
             mock.patch("collector.readers.context.time.monotonic", side_effect=clock, return_value=0.0):
            result = reader._query_window()
        return result, sock

    def test_decodes_a_fragmented_response(self):
        result, sock = self.query([b'{"class":"kitty",', b'"title":"shell"}', b""])
        self.assertEqual(result, "kitty|shell")
        sock.sendall.assert_called_once_with(b"j/activewindow")
        sock.__exit__.assert_called_once()

    def test_empty_object_is_a_known_session_without_a_focused_window(self):
        self.assertEqual(self.query([b"{}", b""])[0], "")

    def test_invalid_responses_remain_unknown(self):
        for payload in (b"", b"[]", b"null", b"false", b'""', b"0", b"{", b"\xff",
                        b'{"class":null}', b'{"title":42}'):
            with self.subTest(payload=payload):
                self.assertIsNone(self.query([payload, b""])[0])

    def test_oversized_response_is_bounded_and_socket_is_closed(self):
        result, sock = self.query([b"x" * 8192] * 100)
        self.assertIsNone(result)
        self.assertEqual(sock.recv.call_count, ContextReader.MAX_WINDOW_BYTES // 8192 + 1)
        sock.__exit__.assert_called_once()

    def test_slow_drip_response_has_a_total_deadline(self):
        result, sock = self.query([b"{", b" "] * 100, clock=[0.0, 0.0, 0.0, 0.0, 0.1, 0.21])
        self.assertIsNone(result)
        self.assertEqual(sock.recv.call_count, 2)
        self.assertAlmostEqual(sock.settimeout.call_args.args[0], 0.1)
        sock.__exit__.assert_called_once()

    def test_socket_disappearing_during_discovery_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            paths = [root / "hypr" / name / ".socket.sock" for name in ("gone", "live")]
            for path in paths:
                path.parent.mkdir(parents=True)
                path.touch()
            original_stat = pathlib.Path.stat

            def racy_stat(path, *args, **kwargs):
                if path == paths[0]:
                    raise FileNotFoundError("session fermée")
                return original_stat(path, *args, **kwargs)

            with mock.patch.dict("os.environ", {"XDG_RUNTIME_DIR": directory}), \
                 mock.patch.object(pathlib.Path, "stat", racy_stat):
                self.assertEqual(ContextReader._hypr_sockets(), [paths[1]])

    def test_inaccessible_session_directory_is_optional(self):
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch.object(pathlib.Path, "glob", side_effect=PermissionError("refusé")):
            self.assertEqual(ContextReader._hypr_sockets(), [])
