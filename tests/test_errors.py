from __future__ import annotations

import unittest

from collector import errors


class ClassifyTest(unittest.TestCase):
    def kind(self, text, level=3):
        rule = errors.classify(text, level)
        return rule.kind if rule else None

    def test_families(self):
        cases = {
            "mce: [Hardware Error]: Machine check events logged": "mce",
            "Machine Check Exception: 5 Bank 4: b200000000070f0f": "mce_uncorrected",
            "Hardware error from APEI Generic Hardware Error Source: 1": "whea",
            "EDAC MC0: 1 CE memory read error on DIMM_A1": "ecc_ce",
            "EDAC MC0: 1 UE memory read error on DIMM_A1": "ecc_ue",
            "Kernel panic - not syncing: Fatal exception": "panic",
            "BUG: unable to handle kernel NULL pointer dereference": "oops",
            "general protection fault: 0000 [#1] SMP": "gpf",
            "watchdog: BUG: soft lockup - CPU#3 stuck for 22s!": "soft_lockup",
            "rcu: INFO: rcu_preempt self-detected stall on CPU": "rcu_stall",
            "INFO: task kworker:42 blocked for more than 120 seconds": "hung_task",
            "Out of memory: Killed process 4242 (firefox)": "oom",
            "NVRM: Xid (PCI:0000:01:00): 79, GPU has fallen off the bus": "gpu_xid",
            "amdgpu 0000:03:00.0: amdgpu: GPU reset begin!": "gpu_reset",
            "amdgpu: [gfxhub] page fault (src_id:0 ring:24)": "gpu_fault",
            "nvme nvme0: I/O 12 QID 3 timeout, aborting": "nvme_error",
            "ata1.00: exception Emask 0x10 SAct 0x0": "ata_error",
            "EXT4-fs error (device nvme0n1p2): ext4_find_entry:1663": "fs_error",
            "pcieport 0000:00:01.1: AER: Uncorrected (Fatal) error": "pcie_fatal",
            "pcieport 0000:00:01.1: AER: Corrected error received": "pcie_corrected",
            "CPU3: Core temperature above threshold, cpu clock throttled": "thermal_trip",
            "PM: suspend of devices aborted after 1234 ms": "suspend_error",
            "iwlwifi 0000:02:00.0: Microcode SW error detected": "firmware_error",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.kind(text), expected)

    def test_specific_rule_wins_over_generic(self):
        # mce_uncorrected doit précéder mce : l'inverse classerait une erreur
        # non corrigée comme bénigne.
        self.assertEqual(
            self.kind("mce: [Hardware Error]: Uncorrected error, CPU 2"),
            "mce_uncorrected",
        )

    def test_catch_all_for_unknown_kernel_errors(self):
        rule = errors.classify("pilote_exotique: chose jamais vue", level=3)
        self.assertEqual(rule.kind, errors.CATCH_ALL_KIND)
        self.assertEqual(rule.severity, errors.ERROR)

    def test_catch_all_escalates_with_priority(self):
        self.assertEqual(errors.classify("truc inconnu", level=1).severity, errors.CRITICAL)

    def test_informational_messages_are_ignored(self):
        self.assertIsNone(errors.classify("usb 1-1: new high-speed USB device", level=6))
        self.assertIsNone(errors.classify("Linux version 6.12.0", level=5))

    def test_benign_messages_are_suppressed_even_at_error_priority(self):
        # Sans cette liste, chaque boot rajouterait les mêmes non-événements ;
        # une constante présente partout n'apprend rien à un modèle.
        for text in (
            "virt/tdx: TDX not supported by the host platform",
            "ACPI: 10 ACPI AML tables successfully acquired and loaded",
            "RAS: Correctable Errors collector initialized.",
            "EDAC amd64: Node 0: DRAM ECC disabled, EDAC drivers are available",
        ):
            with self.subTest(text=text):
                self.assertIsNone(errors.classify(text, level=3))

    def test_benign_does_not_swallow_real_errors(self):
        self.assertEqual(self.kind("ACPI Error: Aborting method \\_SB.PCI0"), "acpi_error")

    def test_no_level_means_rules_only(self):
        self.assertIsNone(errors.classify("message inconnu"))
        self.assertEqual(errors.classify("Kernel panic - not syncing").kind, "panic")


class XidTest(unittest.TestCase):
    def test_known_code_is_translated(self):
        self.assertIn(
            "tombé du bus", errors.describe_xid("NVRM: Xid (PCI:0000:01:00): 79, blah")
        )

    def test_unknown_code_is_reported_raw(self):
        self.assertIn("Xid 250", errors.describe_xid("NVRM: Xid (PCI:0): 250, ?"))

    def test_no_xid(self):
        self.assertIsNone(errors.describe_xid("rien à voir"))


class CatalogueTest(unittest.TestCase):
    def test_every_rule_is_documented_and_compiles(self):
        for rule in errors.RULES:
            with self.subTest(kind=rule.kind):
                self.assertTrue(rule.note.strip(), "règle sans explication")
                self.assertIn(rule.severity, (errors.CRITICAL, errors.ERROR, errors.WARNING))
                self.assertTrue(rule.pattern.pattern)

    def test_catalogue_is_deduplicated_by_kind(self):
        kinds = [k for k, _, _ in errors.catalogue()]
        self.assertEqual(len(kinds), len(set(kinds)))
