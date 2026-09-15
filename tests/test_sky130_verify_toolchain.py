from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sky130_verify import toolchain  # noqa: E402


def _write_executable(path: Path, script: str) -> None:
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class ParseLvsLogTests(unittest.TestCase):
    def test_log_patterns_map_to_verdicts(self):
        cases = (
            ("Circuits match uniquely.\n", "pass"),
            ("Circuits match uniquely.\nWarning: 2 property errors found.\n", "fail"),
            ("netgen: failed pin matching\n", "fail"),
            ("netgen: Circuits do not match\n", "fail"),
            ("netgen: Netlists do not match\n", "fail"),
            ("netgen: some unrelated banner\n", "unknown"),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(toolchain.parse_lvs_log(text), expected)


class AslrGuardCommandTests(unittest.TestCase):
    def test_aslr_guard_command_uses_setarch_only_when_available(self):
        argv = toolchain.aslr_guard_command(["magic", "-x"], disabled=True)
        self.assertEqual(argv, ["magic", "-x"])
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = ""
        try:
            argv = toolchain.aslr_guard_command(["magic", "-x"])
        finally:
            os.environ["PATH"] = old_path
        self.assertEqual(argv, ["magic", "-x"])
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp)
            _write_executable(bindir / "setarch", "#!/bin/sh\nexit 0\n")
            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = f"{bindir}{os.pathsep}{old_path}"
            try:
                argv = toolchain.aslr_guard_command(["magic", "-x"])
            finally:
                os.environ["PATH"] = old_path
        self.assertTrue(argv[0].endswith("setarch"))
        self.assertIn("-R", argv)
        self.assertEqual(argv[-2:], ["magic", "-x"])


class RunDrcAndLvsWithFakeToolsTests(unittest.TestCase):
    """Fake magic/netgen mimicking real output shape; exercises the
    invocation/marker/parsing orchestration, not real geometry."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work_dir = Path(self.tmp.name) / "work"
        self.work_dir.mkdir()
        self.bindir = Path(self.tmp.name) / "bin"
        self.bindir.mkdir()
        self.old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.bindir}{os.pathsep}{self.old_path}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", self.old_path))
        self.magicrc = Path(self.tmp.name) / "fake.magicrc"
        self.magicrc.touch()
        self.pdk_root = Path(self.tmp.name) / "fake_pdk"
        self.pdk_root.mkdir()

    def _install_fake_magic(self, drc_errors: int) -> None:
        script = f"""#!/bin/sh
case "$*" in
  *drc.tcl*)
    echo "DRC_LOAD_OK"
    echo "DRC_RUN_OK"
    echo "Total DRC errors found: {drc_errors}"
    ;;
  *extract.tcl*)
    echo "EXTRACT_LOAD_OK"
    echo "EXTRACT_RUN_OK"
    printf '.subckt %s a b c\\nX1 a b c device\\n.ends\\n' "$SKY130VERIFY_CELL" > "$SKY130VERIFY_CELL.spice"
    ;;
esac
"""
        _write_executable(self.bindir / "magic", script)

    def test_run_drc_parses_reported_error_count(self):
        for errors, verdict in ((0, "pass"), (3, "fail")):
            with self.subTest(errors=errors):
                self._install_fake_magic(drc_errors=errors)
                result = toolchain.run_drc(
                    view=Path("dummy.mag"), cell_format="mag", cell="my_cell",
                    magicrc=self.magicrc, pdk_root=self.pdk_root, pdk_variant="sky130A",
                    work_dir=self.work_dir, disable_aslr_guard=True,
                )
                self.assertTrue(result.ok)
                self.assertEqual(result.error_count, errors)
                self.assertEqual(result.verdict, verdict)

    def test_run_drc_load_failure_is_error(self):
        _write_executable(self.bindir / "magic", "#!/bin/sh\necho 'DRC_LOAD_FAIL boom'\n")
        result = toolchain.run_drc(view=Path("dummy.mag"), cell_format="mag", cell="my_cell",
                                    magicrc=self.magicrc, pdk_root=self.pdk_root, pdk_variant="sky130A",
                                    work_dir=self.work_dir, disable_aslr_guard=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.verdict, "error")
        self.assertIsNone(result.error_count)

    def test_run_extraction_writes_the_expected_spice_file(self):
        self._install_fake_magic(drc_errors=0)
        result = toolchain.run_extraction(view=Path("dummy.mag"), cell_format="mag", cell="my_cell",
                                           magicrc=self.magicrc, pdk_root=self.pdk_root, pdk_variant="sky130A",
                                           work_dir=self.work_dir, disable_aslr_guard=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.spice_path.name, "my_cell.spice")
        self.assertIn("my_cell", result.spice_path.read_text())

    def test_run_lvs_reports_pass_for_matching_circuits(self):
        _write_executable(self.bindir / "netgen", "#!/bin/sh\necho 'Circuits match uniquely.'\n")
        extracted = self.work_dir / "extracted.spice"
        golden = self.work_dir / "golden.spice"
        extracted.write_text(".subckt my_cell a\n.ends\n")
        golden.write_text(".subckt my_cell a\n.ends\n")
        setup = self.work_dir / "setup.tcl"
        setup.touch()
        result = toolchain.run_lvs(extracted_spice=extracted, golden_spice=golden, cell="my_cell",
                                    netgen_setup=setup, pdk_root=self.pdk_root,
                                    pdk_variant="sky130A", work_dir=self.work_dir)
        self.assertEqual(result.verdict, "pass")


_EMPTY_RDB = """<?xml version="1.0" encoding="utf-8"?>
<report-database>
 <description>SKY130 DRC runset</description>
 <categories>
 </categories>
 <items>
 </items>
</report-database>
"""

_RDB_WITH_TWO_VIOLATIONS = """<?xml version="1.0" encoding="utf-8"?>
<report-database>
 <description>SKY130 DRC runset</description>
 <categories>
 </categories>
 <items>
  <item><category>x.1</category></item>
  <item><category>x.2</category></item>
 </items>
</report-database>
"""


class CountRdbViolationsTests(unittest.TestCase):
    """Format taken from a real sky130A_mr.drc run against a real GDS."""

    def test_report_items_are_counted_and_invalid_reports_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases = (
                ("empty", _EMPTY_RDB, 0),
                ("two items", _RDB_WITH_TWO_VIOLATIONS, 2),
                ("invalid XML", "not xml at all <<<", None),
            )
            for name, content, expected in cases:
                with self.subTest(name=name):
                    report = root / f"{name}.xml"
                    report.write_text(content)
                    self.assertEqual(toolchain._count_rdb_violations(report), expected)
            self.assertIsNone(toolchain._count_rdb_violations(root / "missing.xml"))


class RunGdsWriteAndKlayoutDrcWithFakeToolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work_dir = Path(self.tmp.name) / "work"
        self.work_dir.mkdir()
        self.bindir = Path(self.tmp.name) / "bin"
        self.bindir.mkdir()
        self.old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.bindir}{os.pathsep}{self.old_path}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", self.old_path))
        self.magicrc = Path(self.tmp.name) / "fake.magicrc"
        self.magicrc.touch()
        self.pdk_root = Path(self.tmp.name) / "fake_pdk"
        self.pdk_root.mkdir()
        self.deck = Path(self.tmp.name) / "sky130A_mr.drc"
        self.deck.touch()

    def test_run_gds_write_reports_output_or_error(self):
        cases = (
            ("""#!/bin/sh
echo "GDSWRITE_LOAD_OK"
echo "fake gds bytes" > converted.gds
echo "GDSWRITE_OK"
""", True),
            ('#!/bin/sh\necho "GDSWRITE_LOAD_FAIL boom"\n', False),
        )
        for script, expected_ok in cases:
            with self.subTest(ok=expected_ok):
                _write_executable(self.bindir / "magic", script)
                result = toolchain.run_gds_write(
                    view=self.work_dir / "my_cell.mag", cell="my_cell", magicrc=self.magicrc,
                    pdk_root=self.pdk_root, pdk_variant="sky130A", work_dir=self.work_dir,
                )
                self.assertEqual(result.ok, expected_ok)
                if expected_ok:
                    self.assertEqual(result.gds_path.name, "converted.gds")
                else:
                    self.assertIsNone(result.gds_path)

    def test_run_klayout_drc_reports_verdict_or_error(self):
        cases = (
            (f"""#!/bin/sh
for a in "$@"; do
  case "$a" in
    report=*) echo '{_EMPTY_RDB}' > "${{a#report=}}" ;;
  esac
done
""", "pass", 0),
            (f"""#!/bin/sh
for a in "$@"; do
  case "$a" in
    report=*) echo '{_RDB_WITH_TWO_VIOLATIONS}' > "${{a#report=}}" ;;
  esac
done
""", "fail", 2),
            ("#!/bin/sh\nexit 0\n", "error", None),
        )
        gds = self.work_dir / "converted.gds"
        gds.touch()
        for script, verdict, violations in cases:
            with self.subTest(verdict=verdict):
                (self.work_dir / "klayout-drc-report.xml").unlink(missing_ok=True)
                _write_executable(self.bindir / "klayout", script)
                result = toolchain.run_klayout_drc(
                    gds=gds, cell="my_cell", deck=self.deck, work_dir=self.work_dir,
                )
                self.assertEqual(result.verdict, verdict)
                self.assertEqual(result.violation_count, violations)


if __name__ == "__main__":
    unittest.main()
