from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest import mock

from collector import registry
from collector.main import SCHEMA
from collector.readers.amdgpu import AmdGpuReader
from collector.readers.gigabyte_wmi import GigabyteWmiReader
from collector.readers.k10temp import K10TempReader
from collector.readers.nvme import NvmeReader
from collector.storage import Storage

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

    def test_labels_colliding_with_generated_suffixes_keep_all_sensors(self):
        labels = ["Composite3", "Composite", "Composite", "Composite3_2"]
        files = {}
        for index, label in enumerate(labels, 1):
            files[f"temp{index}_input"] = str(index * 1000)
            files[f"temp{index}_label"] = label
        make_hwmon(self.root, 0, "nvme", files)
        reader = NvmeReader()
        self.assertTrue(reader.setup())
        self.assertEqual(len(reader.fields), 4)
        self.assertEqual(sorted(reader.read().values()), [1.0, 2.0, 3.0, 4.0])
        again = NvmeReader()
        self.assertTrue(again.setup())
        self.assertEqual(reader.fields, again.fields)

    def test_long_labels_remain_distinct_and_fit_sql_columns(self):
        make_hwmon(self.root, 0, "k10temp", {
            "temp1_input": "40000", "temp1_label": "a" * 100 + "x",
            "temp2_input": "41000", "temp2_label": "a" * 100 + "y",
        })
        reader = K10TempReader()
        self.assertTrue(reader.setup())
        self.assertEqual(len(set(reader.fields)), 2)
        self.assertTrue(all(len(field) <= 63 for field in reader.fields))
        self.assertEqual(sorted(reader.read().values()), [40.0, 41.0])
        storage = Storage(self.root / "sensors.db", SCHEMA)
        self.addCleanup(storage.close)
        storage.ensure_columns(reader.fields)
        storage.insert_samples([{"ts": 1, "boot_id": "test", **reader.read()}])
        columns = ", ".join(reader.fields)
        self.assertEqual(storage.conn.execute(f"SELECT {columns} FROM samples").fetchall(),
                         [(40.0, 41.0)])

    def test_sensor_disappearing_at_runtime_yields_none(self):
        hwmon = make_hwmon(self.root, 0, "k10temp", {"temp1_input": "44000\n"})
        reader = K10TempReader()
        self.assertTrue(reader.setup())
        (hwmon / "temp1_input").unlink()
        self.assertEqual(reader.read(), {"k10temp_temp1": None})

    def test_power_uses_first_readable_source(self):
        for average in (None, "n/a\n", "7000000\n", "0\n"):
            with self.subTest(average=average):
                hwmon = make_hwmon(self.root, 0, "amdgpu", {
                    "power1_input": "5000000\n",
                    "power1_label": "PPT\n",
                })
                preferred = hwmon / "power1_average"
                if average is not None:
                    preferred.write_text(average)
                reader = AmdGpuReader()
                self.assertTrue(reader.setup())
                expected = 5.0 if average in (None, "n/a\n") else int(average) / 1e6
                self.assertEqual(reader.read(), {"amdgpu_ppt_w": expected})
                # La source choisie au setup reste stable pendant la collecte.
                if average in (None, "n/a\n"):
                    preferred.write_text("9000000\n")
                    self.assertEqual(reader.read(), {"amdgpu_ppt_w": 5.0})
                preferred.unlink(missing_ok=True)

    def test_gpu_utilization_and_colliding_sensor_labels_stay_distinct(self):
        make_hwmon(self.root, 0, "amdgpu", {
            "temp1_input": "42000", "temp1_label": "Busy Pct",
            "temp2_input": "43000", "temp2_label": "Busy Pct 2",
            "device/gpu_busy_percent": "75",
        })
        reader = AmdGpuReader()
        self.assertTrue(reader.setup())
        self.assertEqual(reader.read(), {
            "amdgpu_busy_pct": 75.0,
            "amdgpu_busy_pct_2": 43.0,
            "amdgpu_busy_pct_3": 42.0,
        })
        self.assertEqual(len(reader.fields), len(set(reader.fields)))
