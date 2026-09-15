from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sky130_verify import badge  # noqa: E402


class ColorForVerdictsTests(unittest.TestCase):
    def test_verdict_pairs_select_expected_color(self):
        cases = (
            ("pass", "pass", "brightgreen"),
            ("pass", "fail", "red"),
            ("fail", "pass", "red"),
            ("error", "pass", "red"),
            ("pass", "not_run", "yellow"),
            ("unknown", "unknown", "yellow"),
        )
        for drc, lvs, expected in cases:
            with self.subTest(drc=drc, lvs=lvs):
                self.assertEqual(badge.color_for_verdicts(drc, lvs).name, expected)


class EndpointJsonTests(unittest.TestCase):
    def test_matches_shields_io_endpoint_schema_shape(self):
        data = badge.endpoint_json("my_cell", "pass", "pass")
        self.assertEqual(data["schemaVersion"], 1)
        self.assertEqual(data["label"], "sky130-verify")
        self.assertIn("pass", data["message"])
        self.assertEqual(data["color"], "brightgreen")


class RenderSvgTests(unittest.TestCase):
    def test_svg_is_well_formed_and_contains_label_and_message(self):
        svg = badge.render_svg("sky130-verify", "drc pass · lvs pass", "#4c1")
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn("sky130-verify", svg)
        self.assertIn("drc pass", svg)
        self.assertIn("#4c1", svg)

    def test_svg_escapes_markup_in_message(self):
        svg = badge.render_svg("label", "a < b & c", "#000")
        self.assertNotIn("a < b", svg)
        self.assertIn("&lt;", svg)
        self.assertIn("&amp;", svg)


class RenderAllTests(unittest.TestCase):
    def test_render_all_produces_the_three_documented_artifacts(self):
        files = badge.render_all("my_cell", "pass", "fail")
        self.assertEqual(set(files), {"badge.json", "badge.svg", "snippet.md"})
        self.assertTrue(files["badge.json"].strip().startswith("{"))
        self.assertTrue(files["badge.svg"].strip().startswith("<svg"))
        self.assertIn("my_cell", files["snippet.md"])


if __name__ == "__main__":
    unittest.main()
