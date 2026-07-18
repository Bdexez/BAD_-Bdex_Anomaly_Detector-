from __future__ import annotations

import unittest

from collector.readers.base import Reader, ReaderState


class FakeReader(Reader):
    name = "fake"
    fields = ["a", "b"]

    def __init__(self, results):
        self.results = list(results)
        self.closed = False

    def read(self):
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        self.closed = True


class ReaderStateTest(unittest.TestCase):
    def test_passes_values_through(self):
        state = ReaderState(FakeReader([{"a": 1.0}]))
        self.assertEqual(state.read(), {"a": 1.0})

    def test_drops_undeclared_keys(self):
        # Une clé hors schéma ferait échouer l'INSERT et emporterait le batch
        # entier, donc les données des autres readers avec.
        state = ReaderState(FakeReader([{"a": 1.0, "surprise": 2.0}]))
        self.assertEqual(state.read(), {"a": 1.0})

    def test_disables_after_three_failures(self):
        disabled = []
        state = ReaderState(
            FakeReader([RuntimeError("boom")] * 3),
            on_disable=lambda name, msg: disabled.append((name, msg)),
        )
        for _ in range(3):
            self.assertEqual(state.read(), {})
        self.assertFalse(state.enabled)
        self.assertEqual(len(disabled), 1)
        self.assertEqual(disabled[0][0], "fake")
        self.assertIn("boom", disabled[0][1])
        # Une fois désactivé, plus aucun appel au reader.
        self.assertEqual(state.read(), {})

    def test_failure_counter_resets_on_success(self):
        state = ReaderState(FakeReader([RuntimeError("x"), RuntimeError("x"), {"a": 1.0}, RuntimeError("x")]))
        state.read(), state.read(), state.read(), state.read()
        self.assertTrue(state.enabled)
        self.assertEqual(state.failures, 1)

    def test_non_dict_is_a_failure_not_a_crash(self):
        state = ReaderState(FakeReader([[1, 2, 3]]))
        self.assertEqual(state.read(), {})
        self.assertEqual(state.failures, 1)

    def test_on_disable_exception_does_not_propagate(self):
        def explode(name, message):
            raise OSError("base injoignable")

        state = ReaderState(FakeReader([RuntimeError("boom")] * 3), on_disable=explode)
        for _ in range(3):
            state.read()
        self.assertFalse(state.enabled)

    def test_column_type(self):
        class TextReader(FakeReader):
            text_fields = frozenset({"b"})

        reader = TextReader([])
        self.assertEqual(reader.column_type("a"), "REAL")
        self.assertEqual(reader.column_type("b"), "TEXT")
