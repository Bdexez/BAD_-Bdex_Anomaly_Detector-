from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest import mock

from collector import registry
from collector.readers.gigabyte_wmi import GigabyteWmiReader
from collector.readers.k10temp import K10TempReader
from collector.readers.nvme import NvmeReader

from .helpers import make_hwmon


class HwmonReaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(registry, "HWMON_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_k10temp_uses_labels_and_skips_missing_channels(self):
        # Zen 4 desktop : temp1=Tctl, temp3=Tccd1, pas de temp2.
        make_hwmon(self.root, 3, "k10temp", {
            "temp1_input": "45250\n", "temp1_label": "Tctl\n",
            "temp3_input": "42000\n", "temp3_label": "Tccd1\n",
        })
        reader = K10TempReader()
        self.assertTrue(reader.setup())
        self.assertEqual(reader.fields, ["k10temp_tctl", "k10temp_tccd1"])
        self.assertEqual(reader.read(), {"k10temp_tctl": 45.25, "k10temp_tccd1": 42.0})

    def test_k10temp_without_tccd(self):
        # Zen+ mobile : seulement Tctl. Le schéma doit s'adapter, pas planter.
        make_hwmon(self.root, 0, "k10temp", {"temp1_input": "44000\n", "temp1_label": "Tctl\n"})
        reader = K10TempReader()
        self.assertTrue(reader.setup())
        self.assertEqual(reader.fields, ["k10temp_tctl"])

    def test_absent_hardware_disables_reader(self):
        make_hwmon(self.root, 0, "nvme", {"temp1_input": "40000\n"})
        self.assertFalse(K10TempReader().setup())

    def test_label_only_channel_produces_no_column(self):
        # amdgpu expose in0_label sans in0_input lisible : une colonne de NULL
        # sur 14 jours n'apprend rien à personne.
        make_hwmon(self.root, 0, "k10temp", {"temp1_input": "44000\n", "temp2_label": "vddgfx\n"})
        reader = K10TempReader()
        self.assertTrue(reader.setup())
        self.assertEqual(reader.fields, ["k10temp_temp1"])

    def test_gigabyte_ignores_labels_and_numbers_columns(self):
        files = {f"temp{i}_input": f"{30000 + i}\n" for i in range(1, 7)}
        files["temp1_label"] = "generic\n"
        make_hwmon(self.root, 1, "gigabyte_wmi", files)
        reader = GigabyteWmiReader()
        self.assertTrue(reader.setup())
        self.assertEqual(reader.fields, [f"gigabyte_temp{i}" for i in range(1, 7)])
        self.assertAlmostEqual(reader.read()["gigabyte_temp1"], 30.001)

    def test_duplicate_labels_get_distinct_columns(self):
        make_hwmon(self.root, 0, "nvme", {
            "temp1_input": "40000\n", "temp1_label": "Composite\n",
            "temp2_input": "41000\n", "temp2_label": "Composite\n",
        })
        reader = NvmeReader()
        self.assertTrue(reader.setup())
        self.assertEqual(len(set(reader.fields)), 2)

    def test_second_instance_gets_its_own_prefix(self):
        for index in (0, 1):
            make_hwmon(self.root, index, "nvme", {"temp1_input": f"{40000 + index}\n"})
        first, second = NvmeReader(0), NvmeReader(1)
        self.assertTrue(first.setup())
        self.assertTrue(second.setup())
        self.assertEqual(first.fields, ["nvme_temp1"])
        self.assertEqual(second.fields, ["nvme1_temp1"])
        self.assertFalse(NvmeReader(2).setup())

    def test_sensor_disappearing_at_runtime_yields_none(self):
        hwmon = make_hwmon(self.root, 0, "k10temp", {"temp1_input": "44000\n"})
        reader = K10TempReader()
        self.assertTrue(reader.setup())
        (hwmon / "temp1_input").unlink()
        self.assertEqual(reader.read(), {"k10temp_temp1": None})
