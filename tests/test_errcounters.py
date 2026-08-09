from __future__ import annotations

import pathlib
import tempfile
import unittest

from collector import errors
from collector.readers.errcounters import _interrupt_totals

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


