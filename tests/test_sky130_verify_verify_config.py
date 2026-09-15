from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sky130_verify.verify_config import (  # noqa: E402
    VerifyConfigError,
    find_verify_config,
    load_verify_config,
)


class FindVerifyConfigTests(unittest.TestCase):
    def test_absent_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(find_verify_config(Path(tmp)))

    def test_present_is_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "verify.toml").write_text("")
            self.assertEqual(find_verify_config(Path(tmp)), Path(tmp) / "verify.toml")


class LoadVerifyConfigTests(unittest.TestCase):
    def test_explicit_layout_and_schematic_paths_resolved_relative_to_config_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "layouts").mkdir()
            (root / "netlists").mkdir()
            (root / "layouts" / "real.mag").write_text("x")
            (root / "netlists" / "golden.spice").write_text("x")
            config_path = root / "verify.toml"
            config_path.write_text(
                '[cell]\nlayout = "layouts/real.mag"\nschematic = "netlists/golden.spice"\n'
                'name = "foo"\n[pdk]\nvariant = "sky130A"\n'
            )
            config = load_verify_config(config_path, base_dir=root)
            self.assertEqual(config.layout, (root / "layouts" / "real.mag").resolve())
            self.assertEqual(config.schematic, (root / "netlists" / "golden.spice").resolve())
            self.assertEqual(config.cell, "foo")
            self.assertEqual(config.pdk_variant, "sky130A")

    def test_missing_referenced_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "verify.toml"
            config_path.write_text('[cell]\nlayout = "nope.mag"\n')
            with self.assertRaises(VerifyConfigError):
                load_verify_config(config_path, base_dir=root)

    def test_invalid_toml_syntax_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "verify.toml"
            config_path.write_text("this is not [valid toml")
            with self.assertRaises(VerifyConfigError):
                load_verify_config(config_path, base_dir=root)

    def test_unknown_configuration_fields_are_rejected(self):
        cases = ("[bogus]\nx = 1\n", '[cell]\nbogus_key = "x"\n')
        for content in cases:
            with self.subTest(content=content):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    config_path = root / "verify.toml"
                    config_path.write_text(content)
                    with self.assertRaises(VerifyConfigError):
                        load_verify_config(config_path, base_dir=root)

    def test_empty_config_is_valid_all_fields_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "verify.toml"
            config_path.write_text("")
            config = load_verify_config(config_path, base_dir=root)
            self.assertIsNone(config.layout)
            self.assertIsNone(config.schematic)
            self.assertIsNone(config.cell)

    def test_source_table_fields_are_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "verify.toml"
            config_path.write_text(
                '[source]\nrepository = "https://example.com/repo.git"\ncommit = "' + "a" * 40 + '"\n'
            )
            config = load_verify_config(config_path, base_dir=root)
            self.assertEqual(config.source_repository, "https://example.com/repo.git")
            self.assertEqual(config.source_commit, "a" * 40)


if __name__ == "__main__":
    unittest.main()
