from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest import mock

from collector.readers.rapl import RaplReader

from .helpers import write


class RaplTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def zone(self, dirname: str, name: str, energy: int, maximum: int = 1_000_000):
        zone = self.root / dirname
        write(zone / "name", name + "\n")
        write(zone / "energy_uj", f"{energy}\n")
        write(zone / "max_energy_range_uj", f"{maximum}\n")
        return zone

    def set_energy(self, dirname: str, energy: int):
        (self.root / dirname / "energy_uj").write_text(f"{energy}\n")

    def reader(self, monotonic):
        reader = RaplReader(root=self.root)
        patcher = mock.patch("collector.readers.rapl.time.monotonic", side_effect=monotonic)
        patcher.start()
        self.addCleanup(patcher.stop)
        return reader

    def test_discovery_names_zones(self):
        self.zone("intel-rapl:0", "package-0", 0)
        self.zone("intel-rapl:0:0", "core", 0)
        reader = RaplReader(root=self.root)
        self.assertTrue(reader.setup())
        self.assertEqual(sorted(reader.fields), ["rapl_core_watts", "rapl_pkg_watts"])

    def test_multi_socket_prefixes_packages(self):
        self.zone("intel-rapl:0", "package-0", 0)
        self.zone("intel-rapl:1", "package-1", 0)
        reader = RaplReader(root=self.root)
        self.assertTrue(reader.setup())
        self.assertEqual(sorted(reader.fields), ["rapl_pkg0_watts", "rapl_pkg1_watts"])

    def test_no_zone_disables_reader(self):
        self.assertFalse(RaplReader(root=self.root).setup())

    def test_zone_without_max_is_ignored(self):
        zone = self.zone("intel-rapl:0", "package-0", 0)
        (zone / "max_energy_range_uj").unlink()
        self.assertFalse(RaplReader(root=self.root).setup())

    def test_first_read_has_no_derivative(self):
        self.zone("intel-rapl:0", "package-0", 0)
        reader = self.reader([0.0])
        reader.setup()
        self.assertEqual(reader.read(), {"rapl_pkg_watts": None})

    def test_derives_power(self):
        self.zone("intel-rapl:0", "package-0", 0)
        reader = self.reader([0.0, 1.0])
        reader.setup()
        reader.read()
        self.set_energy("intel-rapl:0", 65_000_000 // 1000 * 1000)  # 65 mJ en 1 s
        self.assertAlmostEqual(reader.read()["rapl_pkg_watts"], 65.0, places=3)

    def test_wrap_does_not_produce_negative_power(self):
        # Le compteur reboucle à 1 J : sans traitement, ΔE = -800 mJ.
        self.zone("intel-rapl:0", "package-0", 900_000)
        reader = self.reader([0.0, 1.0])
        reader.setup()
        reader.read()
        self.set_energy("intel-rapl:0", 100_000)
        self.assertAlmostEqual(reader.read()["rapl_pkg_watts"], 0.2, places=6)

    def test_counter_reset_is_dropped_not_reported(self):
        # Au resume de veille, le compteur repart de zéro alors que
        # CLOCK_MONOTONIC n'a pas avancé : ΔE énorme / Δt minuscule.
        self.zone("intel-rapl:0", "package-0", 0, maximum=10**9)
        reader = self.reader([0.0, 0.001])
        reader.setup()
        reader.read()
        self.set_energy("intel-rapl:0", 10**9 - 1)
        self.assertIsNone(reader.read()["rapl_pkg_watts"])

    def test_unreadable_zone_yields_none_and_resets_state(self):
        self.zone("intel-rapl:0", "package-0", 0)
        reader = self.reader([0.0, 1.0, 2.0])
        reader.setup()
        reader.read()
        (self.root / "intel-rapl:0" / "energy_uj").unlink()
        self.assertEqual(reader.read(), {"rapl_pkg_watts": None})
        # Le point précédent a été oublié : pas de dérivée sur un trou.
        write(self.root / "intel-rapl:0" / "energy_uj", "500000\n")
        self.assertEqual(reader.read(), {"rapl_pkg_watts": None})
