from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest import mock

from collector.readers import proc as proc_module
from collector.readers.context import ContextReader
from collector.readers.proc import ProcReader
from collector.readers.psi import PsiReader

from .helpers import make_proc_stat, write

PSI = """some avg10=1.50 avg60=0.05 avg300=0.09 total=962454
full avg10=0.75 avg60=0.00 avg300=0.00 total=0
"""


class PsiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_parses_some_and_full(self):
        for name in ("cpu", "io", "memory"):
            write(self.root / name, PSI)
        reader = PsiReader(root=self.root)
        self.assertTrue(reader.setup())
        self.assertEqual(
            reader.read(),
            {
                "psi_cpu_some": 1.5,
                "psi_io_some": 1.5,
                "psi_io_full": 0.75,
                "psi_mem_some": 1.5,
                "psi_mem_full": 0.75,
            },
        )

    def test_missing_files_shrink_the_schema(self):
        write(self.root / "cpu", PSI)
        reader = PsiReader(root=self.root)
        self.assertTrue(reader.setup())
        self.assertEqual(reader.fields, ["psi_cpu_some"])

    def test_psi_disabled_kernel(self):
        self.assertFalse(PsiReader(root=self.root).setup())


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


class ContextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        write(self.root / "uptime", "4270.06 30000.00\n")
        patcher = mock.patch.object(ContextReader, "_find_gpu_busy", staticmethod(lambda: None))
        patcher.start()
        self.addCleanup(patcher.stop)

    def add_proc(self, pid, comm, utime, stime, starttime=1):
        write(self.root / str(pid) / "stat", make_proc_stat(pid, comm, utime, stime, starttime))

    def reader(self, window="kitty|shell"):
        reader = ContextReader(proc_root=self.root)
        reader.setup()
        reader._query_window = lambda: window
        return reader

    def test_counts_processes_and_reads_uptime(self):
        self.add_proc(1, "systemd", 0, 0)
        self.add_proc(42, "kitty", 0, 0)
        write(self.root / "self" / "stat", "ignoré")  # /proc/self n'est pas un pid
        out = self.reader().read()
        self.assertEqual(out["n_procs"], 2.0)
        self.assertEqual(out["uptime_s"], 4270.06)
        self.assertIsNone(out["top_proc_cpu"])  # pas de point précédent

    def test_top_process_is_derived_between_ticks(self):
        self.add_proc(1, "systemd", 0, 0)
        self.add_proc(42, "stress-ng", 0, 0)
        reader = self.reader()
        with mock.patch("time.monotonic", side_effect=[0.0, 0.0, 1.0, 1.0]):
            reader.read()
            self.add_proc(42, "stress-ng", 50, 50)  # 100 jiffies en 1 s
            out = reader.read()
        self.assertEqual(out["top_proc_name"], "stress-ng")
        self.assertAlmostEqual(out["top_proc_cpu"], 100.0, places=3)

    def test_process_name_with_spaces_and_parentheses(self):
        self.add_proc(7, "Web Content (x)", 0, 0)
        reader = self.reader()
        with mock.patch("time.monotonic", side_effect=[0.0, 0.0, 1.0, 1.0]):
            reader.read()
            self.add_proc(7, "Web Content (x)", 10, 0, starttime=1)
            out = reader.read()
        self.assertEqual(out["top_proc_name"], "Web Content (x)")

    def test_pid_reuse_is_not_counted_as_cpu_burst(self):
        self.add_proc(9, "old", 5000, 0, starttime=1)
        reader = self.reader()
        with mock.patch("time.monotonic", side_effect=[0.0, 0.0, 1.0, 1.0]):
            reader.read()
            self.add_proc(9, "new", 0, 0, starttime=999)  # même pid, autre process
            out = reader.read()
        self.assertEqual(out["top_proc_cpu"], 0.0)

    def test_is_gaming_flags_known_hints(self):
        self.add_proc(1, "init", 0, 0)
        self.assertEqual(self.reader("steam_app_1234|STALKER 2").read()["is_gaming"], 1.0)
        self.assertEqual(self.reader("kitty|shell").read()["is_gaming"], 0.0)

    def test_is_gaming_is_null_when_compositor_unreachable(self):
        # Écrire 0 ici affirmerait "pas de jeu" alors qu'on ne sait rien :
        # c'est un faux label, et un faux label pollue tout le dataset.
        self.add_proc(1, "init", 0, 0)
        self.assertIsNone(self.reader(window=None).read()["is_gaming"])

    def test_is_compositing_is_null_without_gpu_counter(self):
        self.add_proc(1, "init", 0, 0)
        self.assertIsNone(self.reader().read()["is_compositing"])
