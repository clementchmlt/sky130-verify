from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sky130_verify import badge  # noqa: E402


class ColorForVerdictsTests(unittest.TestCase):
    def test_both_pass_is_green(self):
        self.assertEqual(badge.color_for_verdicts("pass", "pass").name, "brightgreen")

    def test_any_fail_is_red(self):
        self.assertEqual(badge.color_for_verdicts("pass", "fail").name, "red")
        self.assertEqual(badge.color_for_verdicts("fail", "pass").name, "red")

    def test_any_error_is_red(self):
        self.assertEqual(badge.color_for_verdicts("error", "pass").name, "red")

    def test_unknown_or_not_run_without_fail_or_error_is_yellow_not_green(self):
        # Incomplete DRC output is yellow.
        self.assertEqual(badge.color_for_verdicts("pass", "not_run").name, "yellow")
        self.assertEqual(badge.color_for_verdicts("unknown", "unknown").name, "yellow")


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
