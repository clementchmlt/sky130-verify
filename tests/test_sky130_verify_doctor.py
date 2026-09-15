from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sky130_verify import doctor  # noqa: E402


def _write_executable(path: Path, script: str) -> None:
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class ProbeToolTests(unittest.TestCase):
    def test_missing_tool_is_reported_not_found(self):
        status = doctor.probe_tool("a-tool-that-does-not-exist-anywhere")
        self.assertFalse(status.found)
        self.assertIsNone(status.version)
        self.assertIsNone(status.path)

    def test_present_tool_reports_its_first_output_line_as_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp)
            _write_executable(bindir / "magic", "#!/bin/sh\necho 'Magic 8.3.500'\n")
            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = f"{bindir}{os.pathsep}{old_path}"
            try:
                status = doctor.probe_tool("magic")
            finally:
                os.environ["PATH"] = old_path
            self.assertTrue(status.found)
            self.assertEqual(status.version, "Magic 8.3.500")
            self.assertTrue(status.required)

    def test_klayout_is_optional_even_when_absent(self):
        status = doctor.probe_tool("klayout")
        self.assertFalse(status.required)


class ResolvePdkTests(unittest.TestCase):
    def setUp(self):
        self._old_pdk_root = os.environ.pop("PDK_ROOT", None)

    def tearDown(self):
        if self._old_pdk_root is not None:
            os.environ["PDK_ROOT"] = self._old_pdk_root

    def test_no_pdk_root_is_reported_unresolved_with_an_explanatory_note(self):
        status = doctor.resolve_pdk(None, "sky130A")
        self.assertFalse(status.found)
        self.assertIn("PDK_ROOT", status.note)

    def test_pdk_root_missing_open_pdks_layout_is_unresolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = doctor.resolve_pdk(tmp, "sky130A")
            self.assertFalse(status.found)

    def test_pdk_root_with_standard_open_pdks_layout_is_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            magic_dir = root / "sky130A" / "libs.tech" / "magic"
            netgen_dir = root / "sky130A" / "libs.tech" / "netgen"
            magic_dir.mkdir(parents=True)
            netgen_dir.mkdir(parents=True)
            (magic_dir / "sky130A.magicrc").touch()
            (netgen_dir / "sky130A_setup.tcl").touch()

            status = doctor.resolve_pdk(str(root), "sky130A", explicit_commit="a" * 40)
            self.assertTrue(status.found)
            self.assertEqual(status.commit_sha, "a" * 40)
            self.assertTrue(status.magicrc.endswith("sky130A.magicrc"))

    def test_unresolved_commit_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            magic_dir = root / "sky130A" / "libs.tech" / "magic"
            netgen_dir = root / "sky130A" / "libs.tech" / "netgen"
            magic_dir.mkdir(parents=True)
            netgen_dir.mkdir(parents=True)
            (magic_dir / "sky130A.magicrc").touch()
            (netgen_dir / "sky130A_setup.tcl").touch()

            status = doctor.resolve_pdk(str(root), "sky130A")
            self.assertTrue(status.found)
            self.assertIsNone(status.commit_sha)
            self.assertIn("--pdk-commit", status.note)

    def test_pdk_root_environment_variable_is_used_when_flag_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            magic_dir = root / "sky130A" / "libs.tech" / "magic"
            netgen_dir = root / "sky130A" / "libs.tech" / "netgen"
            magic_dir.mkdir(parents=True)
            netgen_dir.mkdir(parents=True)
            (magic_dir / "sky130A.magicrc").touch()
            (netgen_dir / "sky130A_setup.tcl").touch()

            os.environ["PDK_ROOT"] = str(root)
            try:
                status = doctor.resolve_pdk(None, "sky130A")
            finally:
                del os.environ["PDK_ROOT"]
            self.assertTrue(status.found)
            self.assertEqual(status.root, str(root))

    def test_explicit_pdk_root_flag_takes_precedence_over_environment_variable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sky130A" / "libs.tech" / "magic").mkdir(parents=True)
            (root / "sky130A" / "libs.tech" / "netgen").mkdir(parents=True)
            (root / "sky130A" / "libs.tech" / "magic" / "sky130A.magicrc").touch()
            (root / "sky130A" / "libs.tech" / "netgen" / "sky130A_setup.tcl").touch()

            os.environ["PDK_ROOT"] = "/definitely/not/a/pdk/root"
            try:
                status = doctor.resolve_pdk(str(root), "sky130A")
            finally:
                del os.environ["PDK_ROOT"]
            self.assertTrue(status.found)
            self.assertEqual(status.root, str(root))

    def test_explicit_commit_divergent_from_a_detectable_clone_is_not_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sky130A" / "libs.tech" / "magic").mkdir(parents=True)
            (root / "sky130A" / "libs.tech" / "netgen").mkdir(parents=True)
            (root / "sky130A" / "libs.tech" / "magic" / "sky130A.magicrc").touch()
            (root / "sky130A" / "libs.tech" / "netgen" / "sky130A_setup.tcl").touch()
            for args in (("git", "init", "-q"), ("git", "config", "user.email", "test@example.invalid"),
                         ("git", "config", "user.name", "Test"), ("git", "add", "."),
                         ("git", "commit", "-qm", "fixture")):
                subprocess.run(args, cwd=root, check=True)
            status = doctor.resolve_pdk(str(root), "sky130A", explicit_commit="a" * 40)
            self.assertTrue(status.found)
            self.assertFalse(status.commit_verified)
            self.assertIn("does not match", status.note)


class DoctorReportTests(unittest.TestCase):
    def test_report_is_not_ok_when_a_required_tool_is_missing_even_if_pdk_resolved(self):
        report = doctor.run_doctor(None, "sky130A")
        self.assertFalse(report.ok)

    def test_missing_aslr_guard_does_not_block_readiness(self):
        base_kwargs = dict(
            platform_machine="x86_64", platform_system="Linux", python_version="3.12.0",
            tools=(doctor.ToolStatus("magic", True, "/usr/bin/magic", "8.3.500", True),
                   doctor.ToolStatus("netgen", True, "/usr/bin/netgen", "1.5.300", True),
                   doctor.ToolStatus("klayout", False, None, None, False)),
            pdk=doctor.PdkStatus(root="/pdk", variant="sky130A", magicrc="/pdk/a", netgen_setup="/pdk/b",
                                  found=True, commit_sha="a" * 40, note="resolved"),
        )
        with_guard = doctor.DoctorReport(**base_kwargs, aslr_guard_available=True)
        without_guard = doctor.DoctorReport(**base_kwargs, aslr_guard_available=False)
        self.assertTrue(with_guard.ok)
        self.assertEqual(with_guard.ok, without_guard.ok)


class ResolveKlayoutTechTests(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.pop("KLAYOUT_TECH_PATH", None)

    def tearDown(self):
        if self._old is not None:
            os.environ["KLAYOUT_TECH_PATH"] = self._old

    def test_absent_klayout_tech_is_unresolved(self):
        status = doctor.resolve_klayout_tech(None)
        self.assertFalse(status.found)
        report = doctor.run_doctor(None, "sky130A")
        self.assertFalse(report.ok)  # magic/netgen absent here, the real cause
        self.assertIsNotNone(report.klayout_tech)

    def test_deck_present_is_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "drc").mkdir()
            (root / "drc" / "sky130A_mr.drc").touch()
            status = doctor.resolve_klayout_tech(str(root))
            self.assertTrue(status.found)
            self.assertTrue(status.deck_path.endswith("sky130A_mr.drc"))

    def test_environment_variable_is_used_when_flag_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "drc").mkdir()
            (root / "drc" / "sky130A_mr.drc").touch()
            os.environ["KLAYOUT_TECH_PATH"] = str(root)
            try:
                status = doctor.resolve_klayout_tech(None)
            finally:
                del os.environ["KLAYOUT_TECH_PATH"]
            self.assertTrue(status.found)


if __name__ == "__main__":
    unittest.main()
