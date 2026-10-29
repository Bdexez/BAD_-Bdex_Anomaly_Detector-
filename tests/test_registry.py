from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest import mock

from collector import registry
from collector.storage import check_identifier

from .helpers import make_hwmon, write


class SlugTest(unittest.TestCase):
    def test_slug(self):
        self.assertEqual(registry.slug("Tctl"), "tctl")
        self.assertEqual(registry.slug("Composite 0"), "composite_0")
        self.assertEqual(registry.slug("  S0-in  "), "s0_in")
        self.assertEqual(registry.slug("Sensor 1 (edge)"), "sensor_1_edge")

    def test_metric_names_fit_the_storage_contract(self):
        taken = set()
        for raw in ("temp_tctl", "9 volts", "???", "a" * 100 + "x",
                    "a" * 100 + "y", "a" * 100 + "x", "temp_tctl", "temp_tctl_2"):
            with self.subTest(raw=raw):
                field = registry.unique_metric_name(raw, taken)
                self.assertEqual(check_identifier(field), field)
                self.assertNotIn(field, taken)
                taken.add(field)
        self.assertIn("temp_tctl", taken)
        self.assertIn("temp_tctl_2", taken)


class HwmonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(registry, "HWMON_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_numeric_order_not_lexicographic(self):
        for index in (0, 2, 10):
            make_hwmon(self.root, index, f"dev{index}", {})
        self.assertEqual(
            [p.name for p in registry.hwmon_dirs()], ["hwmon0", "hwmon2", "hwmon10"]
        )

    def test_find_by_name_not_by_number(self):
        make_hwmon(self.root, 0, "nvme", {})
        target = make_hwmon(self.root, 1, "k10temp", {})
        self.assertEqual(registry.find_hwmon("k10temp"), target)
        self.assertIsNone(registry.find_hwmon("gigabyte_wmi"))

    def test_duplicate_names_are_all_kept(self):
        make_hwmon(self.root, 0, "nvme", {})
        make_hwmon(self.root, 1, "nvme", {})
        self.assertEqual(len(registry.find_all_hwmon("nvme")), 2)
        self.assertEqual(len(registry.list_hwmon()["nvme"]), 2)

    def test_unreadable_entries_are_skipped(self):
        (self.root / "hwmon0").mkdir()  # pas de fichier `name`
        make_hwmon(self.root, 1, "k10temp", {})
        self.assertEqual(list(registry.list_hwmon()), ["k10temp"])

    def test_read_int_tolerates_garbage(self):
        good = write(self.root / "good", "42\n")
        bad = write(self.root / "bad", "n/a\n")
        self.assertEqual(registry.read_int(good), 42)
        self.assertIsNone(registry.read_int(bad))
        self.assertIsNone(registry.read_int(self.root / "absent"))
