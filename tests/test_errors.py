from __future__ import annotations

import unittest

from collector import errors


class ClassifyTest(unittest.TestCase):
    def kind(self, text, level=3):
        rule = errors.classify(text, level)
        return rule.kind if rule else None


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


