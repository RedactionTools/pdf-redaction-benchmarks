"""Normalisation and the residual-disclosure test.

The cases here are the worked examples from docs/metrics/core.md. If one of them stops
holding, the scorer has stopped implementing the published metric, whatever else still
passes.
"""

from __future__ import annotations

import unittest

from pdfredeval.textmatch import disclosure, lcs, lcs_length, normalize, squeeze


class NormalizeTests(unittest.TestCase):
    def test_case_and_whitespace(self):
        self.assertEqual(normalize("  John   SMITH\n"), "john smith")

    def test_ligatures_expand(self):
        self.assertEqual(normalize("ofﬁce"), "office")
        self.assertEqual(normalize("æther"), "aether")

    def test_invisibles_are_stripped(self):
        """A layout engine's soft hyphen must not hide a value from the leak test."""
        self.assertEqual(normalize("Diff­ie"), "diffie")
        self.assertEqual(normalize("Dif​fie"), "diffie")

    def test_empty(self):
        self.assertEqual(normalize(""), "")


class SqueezeTests(unittest.TestCase):
    def test_stacked_glyphs_join(self):
        """One glyph per line is what an extractor returns for a vertical run."""
        self.assertEqual(squeeze("D i f f i e"), "diffie")
        self.assertEqual(squeeze("6 9 0 7 tail"), "6907 tail")

    def test_word_boundaries_survive(self):
        """The guard against manufacturing adjacencies that the page never had."""
        self.assertEqual(squeeze("+44 7669 074391"), "+44 7669 074391")

    def test_a_short_unit_does_not_match_across_a_space(self):
        found = disclosure(("49786907", "6907"), "phone +44 7669 074391", d_c=4)
        self.assertFalse(found.leaked, "6907 is only adjacent once the space is deleted")


class LcsTests(unittest.TestCase):
    def test_length_and_substring_agree(self):
        self.assertEqual(lcs_length("whitfield", "the thornfield estate"), 5)
        self.assertEqual(lcs("whitfield", "the thornfield estate"), "field")

    def test_empty_sides(self):
        self.assertEqual(lcs_length("", "abc"), 0)
        self.assertEqual(lcs_length("abc", ""), 0)
        self.assertEqual(lcs("", "abc"), "")

    def test_identical(self):
        self.assertEqual(lcs_length("acme", "acme"), 4)


class DisclosureTests(unittest.TestCase):
    """Each test is a worked example from docs/metrics/core.md."""

    def test_surname_survives_an_initial(self):
        """`W. Diffie` passes the whole-string ratio at 0.44 and still identifies."""
        units = ("Whitfield Diffie", "Whitfield", "Diffie")
        found = disclosure(units, "signed: W. Diffie")
        self.assertTrue(found.leaked)
        self.assertEqual(found.unit, "Diffie")
        self.assertEqual(found.grade, "full")

    def test_masked_national_id_leaks_its_tail(self):
        """`XXX-XX-6789` is RDR 0.36 - a pass on the ratio, and a disclosed id."""
        found = disclosure(("123-45-6789", "6789"), "ssn XXX-XX-6789", d_c=4)
        self.assertTrue(found.leaked)
        self.assertEqual(found.unit, "6789")

    def test_email_domain_survives_the_local_part(self):
        units = ("john.smith@acme.com", "john", "smith", "acme")
        found = disclosure(units, "sent from @acme.com", d_c=4)
        self.assertTrue(found.leaked, "the employer survived")

    def test_a_coincidental_run_is_not_a_disclosure(self):
        """`rate` inside `postcrate` is a collision, not a leaked domain."""
        units = ("dermot.ulloa@postcrate.example", "dermot", "ulloa", "postcrate")
        found = disclosure(units, "billed at a steady rate", d_c=4)
        self.assertFalse(found.leaked)

    def test_full_removal_is_clean(self):
        found = disclosure(("Whitfield Diffie", "Diffie"), "signed: [redacted]")
        self.assertFalse(found.leaked)
        self.assertEqual(found.grade, "redacted")

    def test_verbatim_survival_grades_full(self):
        found = disclosure(("Whitfield Diffie",), "by Whitfield Diffie")
        self.assertEqual(found.grade, "full")
        self.assertEqual(found.rdr, 1.0)

    def test_partial_survival_grades_partial(self):
        found = disclosure(("Whitfieldcombe",), "of Whitfield")
        self.assertTrue(found.leaked)
        self.assertEqual(found.grade, "partial")

    def test_l_min_floors_the_proportional_test(self):
        """Short values collide by accident in any page of prose."""
        found = disclosure(("Ann",), "in an annual report", l_min=4)
        self.assertFalse(found.leaked)

    def test_a_declared_short_identifier_still_fires(self):
        """What `D_c` is for: a fragment below `L_min` that a category says identifies."""
        found = disclosure(("XY7",), "plate XY7 seen", l_min=4, d_c=3)
        self.assertTrue(found.leaked)
        self.assertEqual(found.test, "absolute")

    def test_stacked_vertical_text_is_caught(self):
        found = disclosure(("Diffie",), "column: D\ni\nf\nf\ni\ne")
        self.assertTrue(found.leaked, "a stacked run must not read as one character")

    def test_empty_haystack(self):
        self.assertFalse(disclosure(("Diffie",), "").leaked)


