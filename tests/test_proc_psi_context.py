from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest import mock

from collector.readers import proc as proc_module
from collector.readers.proc import ProcReader

from .helpers import make_proc_stat, write

PSI = """some avg10=1.50 avg60=0.05 avg300=0.09 total=962454
full avg10=0.75 avg60=0.00 avg300=0.00 total=0
"""




class ProcTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name) / "proc"
        self.cpu_root = pathlib.Path(self.tmp.name) / "cpu"
        self.addCleanup(self.tmp.cleanup)
        write(self.cpu_root / "cpu0" / "cpufreq" / "cpuinfo_avg_freq", "3600000\n")
        write(self.cpu_root / "cpu1" / "cpufreq" / "cpuinfo_avg_freq", "3400000\n")
        patcher = mock.patch.object(proc_module, "CPU_ROOT", self.cpu_root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.write_stat(idle=1000, busy=0)
        write(self.root / "meminfo", "MemTotal: 8000000 kB\nMemAvailable: 4000000 kB\n"
                                     "SwapTotal: 1024000 kB\nSwapFree: 1024000 kB\n")
        write(self.root / "loadavg", "0.51 0.35 0.18 1/531 13026\n")

    def write_stat(self, idle: int, busy: int):
        # user nice system idle iowait irq softirq steal
        line = lambda name: f"{name} {busy} 0 0 {idle} 0 0 0 0 0 0\n"
        write(self.root / "stat", line("cpu ").replace("cpu  ", "cpu  ") + line("cpu0") + line("cpu1"))

    def test_discovers_thread_count(self):
        reader = ProcReader(root=self.root)
        self.assertTrue(reader.setup())
        self.assertIn("cpu1_util", reader.fields)
        self.assertNotIn("cpu2_util", reader.fields)

    def test_first_read_has_no_utilisation(self):
        reader = ProcReader(root=self.root)
        reader.setup()
        first = reader.read()
        self.assertIsNone(first["cpu_util"])
        self.assertEqual(first["mem_used_mb"], (8000000 - 4000000) / 1024)
        self.assertEqual(first["swap_used_mb"], 0.0)
        self.assertEqual(first["load1"], 0.51)
        self.assertEqual(first["cpu_freq_avg"], 3500.0)

    def test_derives_utilisation(self):
        reader = ProcReader(root=self.root)
        reader.setup()
        reader.read()
        self.write_stat(idle=1075, busy=25)  # 25 jiffies occupés sur 100
        second = reader.read()
        self.assertAlmostEqual(second["cpu_util"], 0.25)
        self.assertAlmostEqual(second["cpu0_util"], 0.25)

    def test_util_is_clamped(self):
        reader = ProcReader(root=self.root)
        reader.setup()
        reader.read()
        self.write_stat(idle=900, busy=200)  # compteur qui recule (hotplug CPU)
        self.assertLessEqual(reader.read()["cpu_util"], 1.0)

    def test_missing_proc_disables_reader(self):
        self.assertFalse(ProcReader(root=self.root / "absent").setup())


