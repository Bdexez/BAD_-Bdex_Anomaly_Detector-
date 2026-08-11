from __future__ import annotations

import pathlib
import tempfile
import unittest

from collector import errors
from collector.readers.errcounters import ErrorCounterReader, _interrupt_totals

from .helpers import write

INTERRUPTS = """           CPU0       CPU1
  9:        123          0   IO-APIC   9-fasteoi   acpi
 NMI:          2          3   Non-maskable interrupts
 TRM:          0          0   Thermal event interrupts
 MCE:          0          0   Machine check exceptions
 ERR:          0
"""


class InterruptParsingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = write(pathlib.Path(self.tmp.name) / "interrupts", INTERRUPTS)

    def test_sums_across_cpus(self):
        self.assertEqual(_interrupt_totals(self.path)["NMI"], 5)

    def test_stops_before_the_description(self):
        # « Machine check exceptions » ne doit pas être avalé comme des chiffres.
        self.assertEqual(_interrupt_totals(self.path)["MCE"], 0)

    def test_numeric_irq_lines_are_skipped(self):
        self.assertNotIn("9", _interrupt_totals(self.path))

    def test_missing_file(self):
        self.assertEqual(_interrupt_totals(pathlib.Path("/absent")), {})


class CounterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.proc = root / "proc"
        self.sys = root / "sys"
        write(self.proc / "interrupts", INTERRUPTS)
        write(self.proc / "vmstat", "nr_free_pages 1000\noom_kill 0\n")
        self.emitted = []

    def reader(self):
        reader = ErrorCounterReader(proc=self.proc, sys_root=self.sys)
        reader.emitter = lambda *args: (self.emitted.append(args), True)[1]
        return reader

    def set_interrupts(self, nmi):
        write(self.proc / "interrupts", INTERRUPTS.replace(
            " NMI:          2          3", f" NMI:          {nmi}          0"
        ))

    def test_discovers_only_what_exists(self):
        reader = self.reader()
        self.assertTrue(reader.setup())
        self.assertIn("err_nmi", reader.fields)
        self.assertIn("err_oom_kill", reader.fields)
        # Ni EDAC, ni AER, ni thermal_throttle dans ce faux /sys.
        self.assertNotIn("err_ecc_ce", reader.fields)
        self.assertIn("err_ecc_ce", reader.missing())

    def test_nonzero_counter_is_announced_at_startup(self):
        # Une machine qui compte déjà 5 NMI depuis le boot doit le dire au
        # premier tick, pas attendre la 6e.
        reader = self.reader()
        reader.setup()
        baseline = [e for e in self.emitted if "au démarrage" in e[3]]
        self.assertTrue(any("err_nmi=5" in e[3] for e in baseline))

    def test_zero_counter_stays_silent(self):
        reader = self.reader()
        reader.setup()
        self.assertFalse(any("err_mce" in e[3] for e in self.emitted))

    def test_delta_is_emitted_and_returned(self):
        reader = self.reader()
        reader.setup()
        self.emitted.clear()
        self.assertEqual(reader.read()["err_nmi"], 0.0)

        self.set_interrupts(9)  # 5 -> 9
        out = reader.read()
        self.assertEqual(out["err_nmi"], 4.0)
        kinds = [(e[1], e[3]) for e in self.emitted]
        self.assertTrue(any(k == "nmi" and "+4" in m for k, m in kinds))

    def test_counter_reset_does_not_produce_a_negative_delta(self):
        # Un périphérique retiré puis remis remet son compteur à zéro : inventer
        # un delta négatif ferait détecter au modèle une anomalie inexistante.
        reader = self.reader()
        reader.setup()
        self.set_interrupts(1)  # 5 -> 1
        self.assertEqual(reader.read()["err_nmi"], 0.0)
        self.set_interrupts(3)
        self.assertEqual(reader.read()["err_nmi"], 2.0)

    def test_oom_counter(self):
        reader = self.reader()
        reader.setup()
        write(self.proc / "vmstat", "nr_free_pages 1000\noom_kill 2\n")
        self.assertEqual(reader.read()["err_oom_kill"], 2.0)
        self.assertIn("oom", [e[1] for e in self.emitted])

    def test_aer_counters_sum_named_values(self):
        write(self.sys / "bus/pci/devices/0000:00:01.0/aer_dev_correctable",
              "RxErr 2\nBadTLP 1\nBadDLLP 0\n")
        write(self.sys / "bus/pci/devices/0000:01:00.0/aer_dev_correctable",
              "RxErr 4\nBadTLP 0\n")
        reader = self.reader()
        reader.setup()
        self.assertIn("err_aer_corr", reader.fields)
        self.assertTrue(any("err_aer_corr=7" in e[3] for e in self.emitted))

    def test_edac_counters(self):
        write(self.sys / "devices/system/edac/mc/mc0/ce_count", "12\n")
        write(self.sys / "devices/system/edac/mc/mc0/ue_count", "0\n")
        reader = self.reader()
        reader.setup()
        self.assertIn("err_ecc_ce", reader.fields)
        self.assertIn("err_ecc_ue", reader.fields)
        ce = [e for e in self.emitted if "err_ecc_ce" in e[3]]
        self.assertEqual(ce[0][1], "ecc_ce")

    def test_nvme_state_change_is_an_event(self):
        state = write(self.sys / "class/nvme/nvme0/state", "live\n")
        reader = self.reader()
        reader.setup()
        self.assertEqual(reader.read()["err_nvme_state"], 0.0)
        state.write_text("resetting\n")
        self.emitted.clear()
        self.assertEqual(reader.read()["err_nvme_state"], 1.0)
        self.assertEqual(self.emitted[0][1], "nvme_error")
        self.assertEqual(self.emitted[0][2], errors.ERROR)

    def test_no_source_at_all_disables_reader(self):
        empty = pathlib.Path(self.tmp.name) / "vide"
        reader = ErrorCounterReader(proc=empty, sys_root=empty)
        self.assertFalse(reader.setup())

    def test_unreadable_counter_yields_none_not_a_crash(self):
        reader = self.reader()
        reader.setup()
        (self.proc / "interrupts").unlink()
        self.assertIsNone(reader.read()["err_nmi"])
