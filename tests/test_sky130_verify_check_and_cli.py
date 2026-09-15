from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sky130_verify import cli, exitcodes  # noqa: E402
from sky130_verify.check import (  # noqa: E402
    UsageError, normalize_repository_url, resolve_cell_inputs, run_check,
)


def _write_executable(path: Path, script: str) -> None:
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


class ResolveCellInputsTests(unittest.TestCase):
    def test_directory_input_is_resolved_or_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            cell_dir = Path(tmp)
            (cell_dir / "foo.mag").touch()
            (cell_dir / "foo.spice").touch()
            inputs = resolve_cell_inputs(cell_dir, cell_override=None, schematic_override=None)
            self.assertEqual(inputs.cell, "foo")
            self.assertEqual(inputs.cell_format, "mag")
        with tempfile.TemporaryDirectory() as tmp:
            cell_dir = Path(tmp)
            (cell_dir / "foo.mag").touch()
            (cell_dir / "bar.gds").touch()
            (cell_dir / "foo.spice").touch()
            with self.assertRaises(UsageError):
                resolve_cell_inputs(cell_dir, cell_override=None, schematic_override=None)
        with tempfile.TemporaryDirectory() as tmp:
            cell_dir = Path(tmp)
            (cell_dir / "foo.mag").touch()
            with self.assertRaises(UsageError):
                resolve_cell_inputs(cell_dir, cell_override=None, schematic_override=None)


class NormalizeRepositoryUrlTests(unittest.TestCase):
    def test_repository_url_normalization(self):
        cases = (
            ("git@github.com:example-org/example-repo.git", "ssh://git@github.com/example-org/example-repo.git"),
            ("https://example.com/repo.git", "https://example.com/repo.git"),
            ("example.com/repo.git", "example.com/repo.git"),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(normalize_repository_url(value), expected)


class RunCheckEndToEndWithFakeToolchainTests(unittest.TestCase):
    """Fake magic/netgen binaries; exercises the full assembly (git ->
    manifest -> badge -> exit code), not the tools themselves."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

        self.repo = self.root / "repo"
        self.cell_dir = self.repo / "cells" / "my_cell"
        self.cell_dir.mkdir(parents=True)
        (self.cell_dir / "my_cell.mag").write_text("fake layout\n")
        (self.cell_dir / "my_cell.spice").write_text(".subckt my_cell a\n.ends\n")
        _git("init", "-q", cwd=self.repo)
        _git("config", "user.email", "test@example.com", cwd=self.repo)
        _git("config", "user.name", "Test", cwd=self.repo)
        _git("add", "-A", cwd=self.repo)
        _git("commit", "-q", "-m", "fixture", cwd=self.repo)
        _git("remote", "add", "origin", "https://example.com/repo.git", cwd=self.repo)

        self.pdk_root = self.root / "pdk"
        magic_dir = self.pdk_root / "sky130A" / "libs.tech" / "magic"
        netgen_dir = self.pdk_root / "sky130A" / "libs.tech" / "netgen"
        magic_dir.mkdir(parents=True)
        netgen_dir.mkdir(parents=True)
        (magic_dir / "sky130A.magicrc").touch()
        (netgen_dir / "sky130A_setup.tcl").touch()

        self.bindir = self.root / "bin"
        self.bindir.mkdir()
        _write_executable(self.bindir / "magic", """#!/bin/sh
case "$*" in
  *drc.tcl*) echo "DRC_LOAD_OK"; echo "DRC_RUN_OK"; echo "Total DRC errors found: 0" ;;
  *extract.tcl*)
    echo "EXTRACT_LOAD_OK"; echo "EXTRACT_RUN_OK"
    printf '.subckt %s a\\n.ends\\n' "$SKY130VERIFY_CELL" > "$SKY130VERIFY_CELL.spice" ;;
esac
""")
        _write_executable(self.bindir / "netgen", "#!/bin/sh\necho 'Circuits match uniquely.'\n")
        self.old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.bindir}{os.pathsep}{self.old_path}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", self.old_path))

    def test_clean_run_produces_a_schema_valid_manifest_and_exit_code_zero(self):
        out_dir = self.repo / "out"
        outcome = run_check(
            target=self.cell_dir, out_dir=out_dir, pdk_root=str(self.pdk_root),
            pdk_variant="sky130A", pdk_commit="a" * 40, cell_override=None,
            schematic_override=None, source_repository=None, source_commit=None,
            dry_run=False, disable_aslr_guard=True,
        )
        self.assertEqual(outcome.exit_code, exitcodes.OK)
        manifest = json.loads((out_dir / "manifest.json").read_text())
        self.assertEqual(manifest["verification"]["drc_verdict"], "pass")
        self.assertEqual(manifest["verification"]["lvs_verdict"], "pass")
        self.assertEqual(manifest["source"]["repository"], "https://example.com/repo.git")
        self.assertTrue((out_dir / "badge.svg").is_file())
        self.assertTrue((out_dir / "badge.json").is_file())
        self.assertIn("magicrc_sha256", manifest["toolchain"]["pdk_files"])
        self.assertIn("netgen_setup_sha256", manifest["toolchain"]["pdk_files"])
        self.assertIn("drc_tcl_sha256", manifest["toolchain"]["scripts"])
        self.assertIn("extract_tcl_sha256", manifest["toolchain"]["scripts"])
        self.assertIn("gdswrite_tcl_sha256", manifest["toolchain"]["scripts"])
        self.assertIn("sha256", manifest["toolchain"]["executables"]["magic"])
        self.assertIn("sha256", manifest["toolchain"]["executables"]["netgen"])
        self.assertIn("out/run.json", manifest["artifacts"])
        self.assertIn("out/logs/extract.log", manifest["artifacts"])
        badge_data = json.loads((out_dir / "badge.json").read_text())
        self.assertEqual(badge_data["manifest_sha256"],
                         hashlib.sha256((out_dir / "manifest.json").read_bytes()).hexdigest())

    def test_relative_out_dir_resolves_for_netgen(self):
        # netgen runs with cwd=work_dir; a relative --out must not leave
        # the extracted SPICE file unresolvable from there.
        _write_executable(self.bindir / "netgen", """#!/bin/sh
extracted=$(echo "$3" | cut -d' ' -f1)
if [ ! -f "$extracted" ]; then
  echo "extracted file not found: $extracted (cwd=$(pwd))"
  exit 1
fi
echo "Circuits match uniquely."
""")
        old_cwd = Path.cwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, old_cwd)
        outcome = run_check(
            target=Path("cells/my_cell"), out_dir=Path("out-relative"),
            pdk_root=str(self.pdk_root), pdk_variant="sky130A", pdk_commit="a" * 40,
            cell_override=None, schematic_override=None,
            source_repository=None, source_commit=None,
            dry_run=False, disable_aslr_guard=True,
        )
        self.assertEqual(outcome.exit_code, exitcodes.OK)
        self.assertEqual(outcome.manifest["verification"]["lvs_verdict"], "pass")

    def test_lvs_comparison_out_file_is_captured_as_an_artifact_when_netgen_writes_one(self):
        _write_executable(self.bindir / "netgen", """#!/bin/sh
echo "Circuits match uniquely."
# argv: -batch lvs "<extracted> <cell>" "<golden> <cell>" <setup> <output.out>
echo "device/pin correspondence" > "$6"
""")
        out_dir = self.repo / "out-with-comparison"
        outcome = run_check(
            target=self.cell_dir, out_dir=out_dir, pdk_root=str(self.pdk_root),
            pdk_variant="sky130A", pdk_commit="a" * 40, cell_override=None,
            schematic_override=None, source_repository=None, source_commit=None,
            dry_run=False, disable_aslr_guard=True,
        )
        self.assertEqual(outcome.exit_code, exitcodes.OK)
        self.assertTrue((out_dir / "logs" / "lvs.out").is_file())
        manifest = json.loads((out_dir / "manifest.json").read_text())
        self.assertTrue(any(key.endswith("logs/lvs.out") for key in manifest["artifacts"]))

    def test_dry_run_touches_nothing_on_disk(self):
        out_dir = self.repo / "out-dry"
        outcome = run_check(
            target=self.cell_dir, out_dir=out_dir, pdk_root=str(self.pdk_root),
            pdk_variant="sky130A", pdk_commit="a" * 40, cell_override=None,
            schematic_override=None, source_repository=None, source_commit=None,
            dry_run=True, disable_aslr_guard=True,
        )
        self.assertEqual(outcome.exit_code, exitcodes.OK)
        self.assertFalse(out_dir.exists())

    def test_dry_run_shows_the_plan_even_without_magic_or_netgen_on_path(self):
        os.environ["PATH"] = self.old_path  # remove the fake magic/netgen
        out_dir = self.repo / "out-dry-no-tools"
        outcome = run_check(
            target=self.cell_dir, out_dir=out_dir, pdk_root=str(self.pdk_root),
            pdk_variant="sky130A", pdk_commit="a" * 40, cell_override=None,
            schematic_override=None, source_repository=None, source_commit=None,
            dry_run=True, disable_aslr_guard=True,
        )
        self.assertEqual(outcome.exit_code, exitcodes.OK)
        self.assertEqual(outcome.status, "dry_run")
        self.assertFalse(out_dir.exists())
        self.assertIn("magic", outcome.message)
        self.assertTrue(any("not resolved" in w and "magic" in w for w in outcome.warnings))

    def test_dry_run_without_pdk_resolved_is_still_environment_incomplete(self):
        outcome = run_check(
            target=self.cell_dir, out_dir=self.repo / "out-dry-no-pdk",
            pdk_root=str(self.root / "no-such-pdk"), pdk_variant="sky130A",
            pdk_commit="a" * 40, cell_override=None, schematic_override=None,
            source_repository=None, source_commit=None,
            dry_run=True, disable_aslr_guard=True,
        )
        self.assertEqual(outcome.exit_code, exitcodes.ENVIRONMENT_INCOMPLETE)

    def test_missing_toolchain_is_environment_incomplete(self):
        os.environ["PATH"] = self.old_path  # remove the fake magic/netgen
        outcome = run_check(
            target=self.cell_dir, out_dir=self.repo / "out2", pdk_root=str(self.pdk_root),
            pdk_variant="sky130A", pdk_commit="a" * 40, cell_override=None,
            schematic_override=None, source_repository=None, source_commit=None,
            dry_run=False, disable_aslr_guard=True,
        )
        self.assertEqual(outcome.exit_code, exitcodes.ENVIRONMENT_INCOMPLETE)

    def test_out_dir_outside_repository_is_a_usage_error(self):
        outcome = run_check(
            target=self.cell_dir, out_dir=Path(tempfile.mkdtemp()), pdk_root=str(self.pdk_root),
            pdk_variant="sky130A", pdk_commit="a" * 40, cell_override=None,
            schematic_override=None, source_repository=None, source_commit=None,
            dry_run=False, disable_aslr_guard=True,
        )
        self.assertEqual(outcome.exit_code, exitcodes.USAGE_ERROR)

    def test_scp_like_remote_is_normalized(self):
        _git("remote", "set-url", "origin", "git@example.com:org/repo.git", cwd=self.repo)
        out_dir = self.repo / "out-ssh"
        outcome = run_check(
            target=self.cell_dir, out_dir=out_dir, pdk_root=str(self.pdk_root),
            pdk_variant="sky130A", pdk_commit="a" * 40, cell_override=None,
            schematic_override=None, source_repository=None, source_commit=None,
            dry_run=False, disable_aslr_guard=True,
        )
        self.assertEqual(outcome.exit_code, exitcodes.OK)
        manifest = json.loads((out_dir / "manifest.json").read_text())
        self.assertEqual(manifest["source"]["repository"], "ssh://git@example.com/org/repo.git")

    def test_cell_outside_any_git_repository_requires_explicit_source_flags(self):
        bare_dir = self.root / "no_git" / "my_cell"
        bare_dir.mkdir(parents=True)
        (bare_dir / "my_cell.mag").write_text("x")
        (bare_dir / "my_cell.spice").write_text(".subckt my_cell a\n.ends\n")

        outcome = run_check(
            target=bare_dir, out_dir=bare_dir / "out", pdk_root=str(self.pdk_root),
            pdk_variant="sky130A", pdk_commit="a" * 40, cell_override=None,
            schematic_override=None, source_repository=None, source_commit=None,
            dry_run=False, disable_aslr_guard=True,
        )
        self.assertEqual(outcome.exit_code, exitcodes.USAGE_ERROR)
        self.assertIn("--source-repository", outcome.message)


class EarlyValidationTests(unittest.TestCase):
    """A malformed argument is rejected before any DRC/LVS run."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cell_dir = Path(self.tmp.name) / "cell"
        cell_dir.mkdir()
        (cell_dir / "foo.mag").touch()
        (cell_dir / "foo.spice").touch()
        self.cell_dir = cell_dir

    def _run(self, **overrides):
        kwargs = dict(
            target=self.cell_dir, out_dir=self.cell_dir / "out", pdk_root="/does/not/matter",
            pdk_variant="sky130A", pdk_commit=None, cell_override=None, schematic_override=None,
            source_repository=None, source_commit=None, dry_run=False, disable_aslr_guard=True,
        )
        kwargs.update(overrides)
        return run_check(**kwargs)

    def test_argument_validation(self):
        rejected = (
            ({"pdk_commit": "not-a-sha"}, "--pdk-commit"),
            ({"source_repository": "https://example.com/x.git", "source_commit": "short"}, "--source-commit"),
            ({"source_repository": "example.com/x.git", "source_commit": "a" * 40}, "--source-repository"),
            ({"cell_override": "my cell; rm -rf /"}, "--cell"),
        )
        for options, expected_message in rejected:
            with self.subTest(options=options):
                outcome = self._run(**options)
                self.assertEqual(outcome.exit_code, exitcodes.USAGE_ERROR)
                self.assertIn(expected_message, outcome.message)

        outcome = self._run(source_repository="git@example.com:org/repo.git", source_commit="a" * 40)
        self.assertNotEqual(outcome.exit_code, exitcodes.USAGE_ERROR)


class RunCheckExceptionEnvelopeTests(unittest.TestCase):
    """Check outcomes for subprocess failures."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cell_dir = self.root / "cell"
        self.cell_dir.mkdir()
        (self.cell_dir / "foo.mag").touch()
        (self.cell_dir / "foo.spice").touch()

        self.pdk_root = self.root / "pdk"
        magic_dir = self.pdk_root / "sky130A" / "libs.tech" / "magic"
        netgen_dir = self.pdk_root / "sky130A" / "libs.tech" / "netgen"
        magic_dir.mkdir(parents=True)
        netgen_dir.mkdir(parents=True)
        (magic_dir / "sky130A.magicrc").touch()
        (netgen_dir / "sky130A_setup.tcl").touch()

        self.bindir = self.root / "bin"
        self.bindir.mkdir()
        _write_executable(self.bindir / "magic", "#!/bin/sh\nexit 0\n")
        _write_executable(self.bindir / "netgen", "#!/bin/sh\nexit 0\n")
        self.old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.bindir}{os.pathsep}{self.old_path}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", self.old_path))

    def _run(self, *, out_dir: Path | None = None):
        return run_check(
            target=self.cell_dir, out_dir=out_dir or self.cell_dir / "out", pdk_root=str(self.pdk_root),
            pdk_variant="sky130A", pdk_commit="a" * 40,
            cell_override=None, schematic_override=None,
            source_repository="https://example.com/repo.git", source_commit="a" * 40,
            dry_run=False, disable_aslr_guard=True,
        )

    def test_tool_exceptions_return_structured_outcomes(self):
        from sky130_verify import toolchain

        cases = (
            (subprocess.TimeoutExpired(cmd=["magic", "-dnull"], timeout=600), "timeout"),
            (OSError("disk full"), "tool_error"),
        )
        for error, expected_status in cases:
            with self.subTest(status=expected_status):
                with unittest.mock.patch.object(toolchain, "run_drc", side_effect=error):
                    outcome = self._run(out_dir=self.cell_dir / f"out-{expected_status}")
                self.assertEqual(outcome.exit_code, exitcodes.TOOL_FAILURE)
                self.assertEqual(outcome.status, expected_status)


_EMPTY_KLAYOUT_RDB = """<?xml version="1.0" encoding="utf-8"?>
<report-database><items>
</items></report-database>
"""


class WithKlayoutCrossCheckIntegrationTests(unittest.TestCase):
    """KLayout cross-check integration."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.cell_dir = self.repo / "cells" / "my_cell"
        self.cell_dir.mkdir(parents=True)
        (self.cell_dir / "my_cell.mag").write_text("fake layout\n")
        (self.cell_dir / "my_cell.spice").write_text(".subckt my_cell a\n.ends\n")
        _git("init", "-q", cwd=self.repo)
        _git("config", "user.email", "test@example.com", cwd=self.repo)
        _git("config", "user.name", "Test", cwd=self.repo)
        _git("add", "-A", cwd=self.repo)
        _git("commit", "-q", "-m", "fixture", cwd=self.repo)
        _git("remote", "add", "origin", "https://example.com/repo.git", cwd=self.repo)

        self.pdk_root = self.root / "pdk"
        (self.pdk_root / "sky130A" / "libs.tech" / "magic").mkdir(parents=True)
        (self.pdk_root / "sky130A" / "libs.tech" / "netgen").mkdir(parents=True)
        (self.pdk_root / "sky130A" / "libs.tech" / "magic" / "sky130A.magicrc").touch()
        (self.pdk_root / "sky130A" / "libs.tech" / "netgen" / "sky130A_setup.tcl").touch()

        self.klayout_tech = self.root / "klayout_tech"
        (self.klayout_tech / "drc").mkdir(parents=True)
        (self.klayout_tech / "drc" / "sky130A_mr.drc").touch()

        self.bindir = self.root / "bin"
        self.bindir.mkdir()
        _write_executable(self.bindir / "magic", """#!/bin/sh
case "$*" in
  *drc.tcl*) echo "DRC_LOAD_OK"; echo "DRC_RUN_OK"; echo "Total DRC errors found: 1" ;;
  *extract.tcl*)
    echo "EXTRACT_LOAD_OK"; echo "EXTRACT_RUN_OK"
    printf '.subckt %s a\\n.ends\\n' "$SKY130VERIFY_CELL" > "$SKY130VERIFY_CELL.spice" ;;
  *gdswrite.tcl*) echo "GDSWRITE_LOAD_OK"; echo "fake" > converted.gds; echo "GDSWRITE_OK" ;;
esac
""")
        _write_executable(self.bindir / "netgen", "#!/bin/sh\necho 'Circuits match uniquely.'\n")
        _write_executable(self.bindir / "klayout", f"""#!/bin/sh
for a in "$@"; do
  case "$a" in
    report=*) echo '{_EMPTY_KLAYOUT_RDB}' > "${{a#report=}}" ;;
  esac
done
""")
        self.old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.bindir}{os.pathsep}{self.old_path}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", self.old_path))

    def _run(self, **overrides):
        kwargs = dict(
            target=self.cell_dir, out_dir=self.repo / "out", pdk_root=str(self.pdk_root),
            pdk_variant="sky130A", pdk_commit="a" * 40, cell_override=None,
            schematic_override=None, source_repository=None, source_commit=None,
            dry_run=False, disable_aslr_guard=True, with_klayout=True,
            klayout_tech=str(self.klayout_tech),
        )
        kwargs.update(overrides)
        return run_check(**kwargs)

    def test_klayout_cross_check_preserves_primary_verdicts(self):
        # Magic DRC=fail, KLayout=pass: a deliberate disagreement.
        outcome = self._run()
        self.assertEqual(outcome.exit_code, exitcodes.NOT_CLEAN)  # driven by Magic DRC alone
        self.assertEqual(outcome.manifest["verification"]["drc_verdict"], "fail")
        self.assertEqual(outcome.manifest["verification"]["lvs_verdict"], "pass")
        cross_check = outcome.manifest["toolchain"]["klayout_cross_check"]
        self.assertEqual(cross_check["drc_verdict"], "pass")
        self.assertEqual(cross_check["violation_count"], 0)

    def test_missing_klayout_tech_is_environment_incomplete(self):
        outcome = self._run(klayout_tech=str(self.root / "does-not-exist"))
        self.assertEqual(outcome.exit_code, exitcodes.ENVIRONMENT_INCOMPLETE)

    def test_without_the_flag_no_cross_check_appears_at_all(self):
        outcome = self._run(with_klayout=False)
        self.assertNotIn("klayout_cross_check", outcome.manifest["toolchain"])

    def test_changed_gdswrite_script_invalidates_conversion_cache(self):
        from sky130_verify import toolchain

        tcl_dir = self.root / "tcl"
        tcl_dir.mkdir()
        for name in ("drc.tcl", "extract.tcl", "gdswrite.tcl"):
            (tcl_dir / name).write_text(name + "\n")
        invocation_log = self.root / "magic-invocations.log"
        _write_executable(self.bindir / "magic", f"""#!/bin/sh
echo "$*" >> {invocation_log}
case "$*" in
  *drc.tcl*) echo "DRC_LOAD_OK"; echo "DRC_RUN_OK"; echo "Total DRC errors found: 1" ;;
  *extract.tcl*)
    echo "EXTRACT_LOAD_OK"; echo "EXTRACT_RUN_OK"
    printf '.subckt %s a\\n.ends\\n' "$SKY130VERIFY_CELL" > "$SKY130VERIFY_CELL.spice" ;;
  *gdswrite.tcl*) echo "GDSWRITE_LOAD_OK"; echo "fake" > converted.gds; echo "GDSWRITE_OK" ;;
esac
""")
        with unittest.mock.patch.object(toolchain, "TCL_DIR", tcl_dir):
            self.assertEqual(self._run().exit_code, exitcodes.NOT_CLEAN)
            self.assertEqual(self._run().exit_code, exitcodes.NOT_CLEAN)
            gdswrite_calls = lambda: sum(
                "gdswrite.tcl" in line for line in invocation_log.read_text().splitlines()
            )
            self.assertEqual(gdswrite_calls(), 1)
            (tcl_dir / "gdswrite.tcl").write_text("revised gds conversion\n")
            self.assertEqual(self._run().exit_code, exitcodes.NOT_CLEAN)
            self.assertEqual(gdswrite_calls(), 2)


class VerifyTomlIntegrationTests(unittest.TestCase):
    """verify.toml fills in what convention-based discovery can't
    resolve; still always one complete cell."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.cell_dir = self.repo / "cell"
        (self.cell_dir / "layouts").mkdir(parents=True)
        (self.cell_dir / "netlists").mkdir(parents=True)
        (self.cell_dir / "layouts" / "real.mag").write_text("v1\n")
        (self.cell_dir / "layouts" / "decoy.mag").write_text("decoy\n")
        (self.cell_dir / "netlists" / "golden.spice").write_text(".subckt foo a\n.ends\n")
        _git("init", "-q", cwd=self.repo)
        _git("config", "user.email", "t@t.com", cwd=self.repo)
        _git("config", "user.name", "T", cwd=self.repo)
        _git("add", "-A", cwd=self.repo)
        _git("commit", "-q", "-m", "fixture", cwd=self.repo)
        _git("remote", "add", "origin", "https://example.com/x.git", cwd=self.repo)

        self.pdk_root = self.root / "pdk"
        (self.pdk_root / "sky130A" / "libs.tech" / "magic").mkdir(parents=True)
        (self.pdk_root / "sky130A" / "libs.tech" / "netgen").mkdir(parents=True)
        (self.pdk_root / "sky130A" / "libs.tech" / "magic" / "sky130A.magicrc").touch()
        (self.pdk_root / "sky130A" / "libs.tech" / "netgen" / "sky130A_setup.tcl").touch()

        self.bindir = self.root / "bin"
        self.bindir.mkdir()
        _write_executable(self.bindir / "magic", """#!/bin/sh
case "$*" in
  *drc.tcl*) echo "DRC_LOAD_OK"; echo "DRC_RUN_OK"; echo "Total DRC errors found: 0" ;;
  *extract.tcl*)
    echo "EXTRACT_LOAD_OK"; echo "EXTRACT_RUN_OK"
    printf '.subckt %s a\\n.ends\\n' "$SKY130VERIFY_CELL" > "$SKY130VERIFY_CELL.spice" ;;
esac
""")
        _write_executable(self.bindir / "netgen", "#!/bin/sh\necho 'Circuits match uniquely.'\n")
        self.old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.bindir}{os.pathsep}{self.old_path}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", self.old_path))

    def _write_config(self, extra: str = "") -> None:
        (self.cell_dir / "verify.toml").write_text(
            '[cell]\nlayout = "layouts/real.mag"\nschematic = "netlists/golden.spice"\n'
            f'name = "foo"\n{extra}'
        )

    def test_layout_and_schematic_outside_target_root_are_found_via_config(self):
        self._write_config()
        outcome = run_check(
            target=self.cell_dir, out_dir=self.repo / "out", pdk_root=str(self.pdk_root),
            pdk_variant=None, pdk_commit="a" * 40, cell_override=None, schematic_override=None,
            source_repository=None, source_commit=None, dry_run=False, disable_aslr_guard=True,
        )
        self.assertEqual(outcome.exit_code, exitcodes.OK)
        self.assertEqual(outcome.manifest["cell_id"], "foo")

    def test_explicit_cli_flag_takes_precedence_over_verify_toml(self):
        self._write_config()
        (self.cell_dir / "netlists" / "other.spice").write_text(".subckt bar a\n.ends\n")
        outcome = run_check(
            target=self.cell_dir, out_dir=self.repo / "out2", pdk_root=str(self.pdk_root),
            pdk_variant=None, pdk_commit="a" * 40, cell_override="bar",
            schematic_override=self.cell_dir / "netlists" / "other.spice",
            source_repository=None, source_commit=None, dry_run=False, disable_aslr_guard=True,
        )
        self.assertEqual(outcome.exit_code, exitcodes.OK)
        self.assertEqual(outcome.manifest["cell_id"], "bar")

    def test_invalid_verify_toml_is_a_usage_error(self):
        (self.cell_dir / "verify.toml").write_text("[cell]\nlayout = \"does-not-exist.mag\"\n")
        outcome = run_check(
            target=self.cell_dir, out_dir=self.repo / "out3", pdk_root=str(self.pdk_root),
            pdk_variant=None, pdk_commit="a" * 40, cell_override=None, schematic_override=None,
            source_repository=None, source_commit=None, dry_run=False, disable_aslr_guard=True,
        )
        self.assertEqual(outcome.exit_code, exitcodes.USAGE_ERROR)

    def test_pdk_variant_from_config_is_used_when_flag_absent(self):
        self._write_config('[pdk]\nvariant = "sky130A"\n')
        outcome = run_check(
            target=self.cell_dir, out_dir=self.repo / "out4", pdk_root=str(self.pdk_root),
            pdk_variant=None, pdk_commit="a" * 40, cell_override=None, schematic_override=None,
            source_repository=None, source_commit=None, dry_run=False, disable_aslr_guard=True,
        )
        self.assertEqual(outcome.exit_code, exitcodes.OK)
        self.assertEqual(outcome.manifest["pdk"]["variant"], "sky130A")


class CheckJsonAlwaysStructuredTests(unittest.TestCase):
    """--json produces the same key contract on every exit path."""

    def _run_json(self, argv: list[str]) -> tuple[int, dict]:
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(argv)
        return code, json.loads(buf.getvalue())

    def test_usage_error_path_is_structured_json_not_prose(self):
        code, data = self._run_json(["check", "/nowhere/at/all", "--json"])
        self.assertEqual(code, exitcodes.USAGE_ERROR)
        self.assertEqual(data["exit_code"], exitcodes.USAGE_ERROR)
        self.assertEqual(data["status"], "usage_error")
        self.assertIsNone(data["manifest"])
        self.assertIn("warnings", data)

    def test_environment_incomplete_path_is_structured_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            cell_dir = Path(tmp)
            (cell_dir / "foo.mag").touch()
            (cell_dir / "foo.spice").touch()
            code, data = self._run_json([
                "check", str(cell_dir), "--pdk-root", "/nowhere", "--json",
                "--source-repository", "https://example.com/x.git",
                "--source-commit", "a" * 40,
            ])
        self.assertEqual(code, exitcodes.ENVIRONMENT_INCOMPLETE)
        self.assertEqual(data["status"], "environment_incomplete")
        self.assertIsNone(data["manifest"])


class CachingAndResumeTests(unittest.TestCase):
    """A second `check` on unchanged inputs re-invokes neither tool; a
    changed input invalidates the cache."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.cell_dir = self.repo / "cell"
        self.cell_dir.mkdir(parents=True)
        (self.cell_dir / "foo.mag").write_text("v1\n")
        (self.cell_dir / "foo.spice").write_text(".subckt foo a\n.ends\n")
        _git("init", "-q", cwd=self.repo)
        _git("config", "user.email", "t@t.com", cwd=self.repo)
        _git("config", "user.name", "T", cwd=self.repo)
        _git("add", "-A", cwd=self.repo)
        _git("commit", "-q", "-m", "fixture", cwd=self.repo)
        _git("remote", "add", "origin", "https://example.com/x.git", cwd=self.repo)

        self.pdk_root = self.root / "pdk"
        (self.pdk_root / "sky130A" / "libs.tech" / "magic").mkdir(parents=True)
        (self.pdk_root / "sky130A" / "libs.tech" / "netgen").mkdir(parents=True)
        (self.pdk_root / "sky130A" / "libs.tech" / "magic" / "sky130A.magicrc").touch()
        (self.pdk_root / "sky130A" / "libs.tech" / "netgen" / "sky130A_setup.tcl").touch()

        self.bindir = self.root / "bin"
        self.bindir.mkdir()
        self.invocation_log = self.root / "invocations.log"
        _write_executable(self.bindir / "magic", f"""#!/bin/sh
echo "magic $*" >> {self.invocation_log}
case "$*" in
  *drc.tcl*) echo "DRC_LOAD_OK"; echo "DRC_RUN_OK"; echo "Total DRC errors found: 0" ;;
  *extract.tcl*)
    echo "EXTRACT_LOAD_OK"; echo "EXTRACT_RUN_OK"
    printf '.subckt %s a\\n.ends\\n' "$SKY130VERIFY_CELL" > "$SKY130VERIFY_CELL.spice" ;;
esac
""")
        _write_executable(self.bindir / "netgen", f"""#!/bin/sh
echo "netgen $*" >> {self.invocation_log}
echo "Circuits match uniquely."
""")
        self.old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.bindir}{os.pathsep}{self.old_path}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", self.old_path))

    def _run(self, **overrides):
        kwargs = dict(
            target=self.cell_dir, out_dir=self.repo / "out", pdk_root=str(self.pdk_root),
            pdk_variant="sky130A", pdk_commit="a" * 40, cell_override=None, schematic_override=None,
            source_repository=None, source_commit=None, dry_run=False, disable_aslr_guard=True,
        )
        kwargs.update(overrides)
        return run_check(**kwargs)

    def _count_real_invocations(self) -> int:
        # Counts only DRC/extraction/LVS calls, not the version probes.
        if not self.invocation_log.is_file():
            return 0
        text = self.invocation_log.read_text()
        return sum(1 for line in text.splitlines() if "drc.tcl" in line or "extract.tcl" in line
                   or "lvs" in line)

    def test_second_identical_run_reuses_drc_extraction_and_lvs(self):
        first = self._run()
        self.assertEqual(first.exit_code, exitcodes.OK)
        count_after_first = self._count_real_invocations()
        self.assertGreater(count_after_first, 0)

        second = self._run()
        self.assertEqual(second.exit_code, exitcodes.OK)
        self.assertEqual(self._count_real_invocations(), count_after_first,
                          "a second identical run should have nothing to re-run")

        run_log = json.loads((self.repo / "out" / "run.json").read_text())
        self.assertTrue(all(step["cached"] for step in run_log["steps"]))

    def test_changed_layout_invalidates_the_cache(self):
        self._run()
        count_after_first = self._count_real_invocations()

        (self.cell_dir / "foo.mag").write_text("v2 — modified content\n")
        second = self._run()
        self.assertEqual(second.exit_code, exitcodes.OK)
        self.assertGreater(self._count_real_invocations(), count_after_first,
                            "modified input content should re-run DRC/extraction/LVS")

    def test_changed_pdk_deck_invalidates_the_cache(self):
        self._run()
        count_after_first = self._count_real_invocations()

        (self.pdk_root / "sky130A" / "libs.tech" / "magic" / "sky130A.magicrc").write_text(
            "# modified deck\n"
        )
        second = self._run()
        self.assertEqual(second.exit_code, exitcodes.OK)
        self.assertGreater(self._count_real_invocations(), count_after_first,
                            "a modified magicrc should re-run DRC/extraction (LVS stays "
                            "indirectly invalidated via extraction)")

    def test_no_cache_flag_forces_full_reexecution(self):
        self._run()
        count_after_first = self._count_real_invocations()
        second = self._run(use_cache=False)
        self.assertEqual(second.exit_code, exitcodes.OK)
        self.assertGreater(self._count_real_invocations(), count_after_first)
        run_log = json.loads((self.repo / "out" / "run.json").read_text())
        self.assertFalse(any(step["cached"] for step in run_log["steps"]))

    def test_interrupted_marker_requires_explicit_resume_then_reuses_safe_cache(self):
        first = self._run()
        self.assertEqual(first.exit_code, exitcodes.OK)
        count_after_first = self._count_real_invocations()
        # Simulates a Ctrl-C left between two steps.
        (self.repo / "out" / "run.json").write_text(json.dumps({"state": "running", "steps": []}))
        refused = self._run()
        self.assertEqual(refused.exit_code, exitcodes.USAGE_ERROR)
        self.assertIn("--resume", refused.message)

        resumed = self._run(resume=True)
        self.assertEqual(resumed.exit_code, exitcodes.OK)
        self.assertEqual(self._count_real_invocations(), count_after_first)
        self.assertEqual(json.loads((self.repo / "out" / "run.json").read_text())["state"], "completed")


class DirtyWorkingTreeWarningTests(unittest.TestCase):
    def test_uncommitted_edit_to_verified_file_produces_a_visible_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            cell_dir = repo / "cell"
            cell_dir.mkdir(parents=True)
            (cell_dir / "foo.mag").write_text("v1\n")
            (cell_dir / "foo.spice").write_text(".subckt foo a\n.ends\n")
            _git("init", "-q", cwd=repo)
            _git("config", "user.email", "t@t.com", cwd=repo)
            _git("config", "user.name", "T", cwd=repo)
            _git("add", "-A", cwd=repo)
            _git("commit", "-q", "-m", "fixture", cwd=repo)
            _git("remote", "add", "origin", "https://example.com/x.git", cwd=repo)

            (cell_dir / "foo.mag").write_text("v2 - not committed\n")

            pdk_root = root / "pdk"
            (pdk_root / "sky130A" / "libs.tech" / "magic").mkdir(parents=True)
            (pdk_root / "sky130A" / "libs.tech" / "netgen").mkdir(parents=True)
            (pdk_root / "sky130A" / "libs.tech" / "magic" / "sky130A.magicrc").touch()
            (pdk_root / "sky130A" / "libs.tech" / "netgen" / "sky130A_setup.tcl").touch()

            outcome = run_check(
                target=cell_dir, out_dir=repo / "out", pdk_root=str(pdk_root),
                pdk_variant="sky130A", pdk_commit="a" * 40, cell_override=None,
                schematic_override=None, source_repository=None, source_commit=None,
                dry_run=False, disable_aslr_guard=True,
            )
            self.assertTrue(any("foo.mag" in w and "differs" in w for w in outcome.warnings))


class CliExitCodeTests(unittest.TestCase):
    def test_doctor_returns_environment_incomplete_when_no_pdk_and_no_tools(self):
        code = cli.main(["doctor", "--pdk-root", "/definitely/not/a/pdk"])
        self.assertEqual(code, exitcodes.ENVIRONMENT_INCOMPLETE)

    def test_doctor_json_uses_the_common_envelope_when_environment_is_incomplete(self):
        import contextlib
        import io

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.main(["doctor", "--pdk-root", "/definitely/not/a/pdk", "--json"])
        data = json.loads(stdout.getvalue())
        self.assertEqual(code, exitcodes.ENVIRONMENT_INCOMPLETE)
        self.assertEqual(data["exit_code"], exitcodes.ENVIRONMENT_INCOMPLETE)
        self.assertEqual(data["status"], "environment_incomplete")
        self.assertIn("report", data)

    def test_batch_subcommand_is_wired_and_reaches_run_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            cell_dir = repo / "cell"
            cell_dir.mkdir(parents=True)
            (cell_dir / "x.mag").touch()
            (cell_dir / "x.spice").touch()
            _git("init", "-q", cwd=repo)
            _git("config", "user.email", "t@t.com", cwd=repo)
            _git("config", "user.name", "T", cwd=repo)
            _git("add", "-A", cwd=repo)
            _git("commit", "-q", "-m", "fixture", cwd=repo)
            _git("remote", "add", "origin", "https://example.com/x.git", cwd=repo)
            code = cli.main(["batch", str(cell_dir), "--pdk-root", "/definitely/not/a/pdk"])
        self.assertEqual(code, exitcodes.ENVIRONMENT_INCOMPLETE)

    def test_manifest_validate_reports_missing_file(self):
        code = cli.main(["manifest", "validate", "/definitely/not/a/file.json"])
        self.assertEqual(code, exitcodes.NOT_CLEAN)

    def test_check_on_nonexistent_path_is_a_usage_error(self):
        code = cli.main(["check", "/definitely/not/a/cell/path"])
        self.assertEqual(code, exitcodes.USAGE_ERROR)

    def test_version_flag_prints_and_exits_zero(self):
        with self.assertRaises(SystemExit) as ctx:
            cli.main(["--version"])
        self.assertEqual(ctx.exception.code, 0)

    def test_keyboard_interrupt_exits_130(self):
        with unittest.mock.patch.object(cli, "_cmd_check", side_effect=KeyboardInterrupt):
            code = cli.main(["check", "/does/not/matter"])
        self.assertEqual(code, 130)


if __name__ == "__main__":
    unittest.main()
