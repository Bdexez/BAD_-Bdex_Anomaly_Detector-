from __future__ import annotations

import unittest
from unittest import mock

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
    def test_rejects_malformed_or_reserved_field_declarations(self):
        for fields in ("a", [None], ["a", "a"], ["not a column"], ["a" * 64],
                       ["ts"], ["boot_id"], ["sample_id"]):
            with self.subTest(fields=fields):
                reader = FakeReader([])
                reader.fields = fields
                with self.assertRaises(ValueError):
                    ReaderState(reader)

    def test_rejects_inconsistent_types(self):
        for text_fields, kind in (({"unknown"}, "REAL"), ("b", "REAL"),
                                  (set(), "BLOB"), (set(), "TEXT"), ({"a"}, "REAL")):
            with self.subTest(text_fields=text_fields, kind=kind):
                reader = FakeReader([])
                reader.text_fields = text_fields
                reader.column_type = lambda field: kind
                with self.assertRaises(ValueError):
                    ReaderState(reader)

    def test_contract_is_frozen_after_setup(self):
        reader = FakeReader([{"a": 1.0, "b": "still text", "ts": 0}])
        reader.text_fields = {"b"}
        state = ReaderState(reader)
        reader.fields = ["ts"]
        reader.text_fields.clear()
        self.assertEqual(state.fields, ("a", "b"))
        self.assertEqual(state.column_types, {"a": "REAL", "b": "TEXT"})
        self.assertEqual(state.read(), {"a": 1.0, "b": "still text"})

    def test_passes_values_through(self):
        state = ReaderState(FakeReader([{"a": 1.0}]))
        self.assertEqual(state.read(), {"a": 1.0})

    def test_drops_undeclared_keys(self):
        # Une clé hors schéma ferait échouer l'INSERT et emporterait le batch
        # entier, donc les données des autres readers avec.
        state = ReaderState(FakeReader([{"a": 1.0, "surprise": 2.0}]))
        self.assertEqual(state.read(), {"a": 1.0})

    def test_invalid_values_leave_other_metrics_available(self):
        for value in ([], {}, object(), "12.5", float("nan"), float("inf"),
                      -float("inf"), 2**63, -(2**63) - 1, 10**1000):
            with self.subTest(value_type=type(value).__name__):
                state = ReaderState(FakeReader([{"a": value, "b": 42.0}]))
                self.assertEqual(state.read(), {"a": None, "b": 42.0})
                self.assertTrue(state.enabled)
                self.assertEqual(state.failures, 0)

    def test_valid_numeric_values_are_preserved(self):
        for value in (None, 0, True, 1.25, -(2**63), 2**63 - 1):
            with self.subTest(value=value):
                state = ReaderState(FakeReader([{"a": value}]))
                self.assertEqual(state.read(), {"a": value})

    def test_text_fields_accept_only_text_or_null(self):
        class TextReader(FakeReader):
            text_fields = frozenset({"b"})

        state = ReaderState(TextReader([{"b": "42"}, {"b": 42}, {"b": None}]))
        self.assertEqual(state.read(), {"b": "42"})
        self.assertEqual(state.read(), {"b": None})
        self.assertEqual(state.read(), {"b": None})

    def test_invalid_value_warning_is_not_repeated_every_tick(self):
        state = ReaderState(FakeReader([{"a": []}] * 3))
        with mock.patch("collector.readers.base.log.warning") as warning:
            for _ in range(3):
                state.read()
        warning.assert_called_once()

    def test_mixed_unknown_key_types_are_dropped_without_a_crash(self):
        state = ReaderState(FakeReader([{"a": 1.0, "surprise": 2.0, 123: 3.0}]))
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
