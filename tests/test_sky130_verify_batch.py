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

from sky130_verify import exitcodes  # noqa: E402
from sky130_verify.batch import _unique_labels, run_batch  # noqa: E402


def _write_executable(path: Path, script: str) -> None:
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


class UniqueLabelsTests(unittest.TestCase):
    def test_distinct_names_are_unchanged(self):
        labels = _unique_labels((Path("cells/inv"), Path("cells/nand2")))
        self.assertEqual(labels, ["inv", "nand2"])

    def test_colliding_basenames_are_disambiguated_not_silently_overwritten(self):
        labels = _unique_labels((Path("libA/inv"), Path("libB/inv")))
        self.assertEqual(len(set(labels)), 2)


class RunBatchWithFakeToolchainTests(unittest.TestCase):
    """One clean cell and one broken cell: the overall exit code must
    reflect the worse of the two."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        for name in ("inv", "nand2"):
            cell_dir = self.repo / "cells" / name
            cell_dir.mkdir(parents=True)
            (cell_dir / f"{name}.mag").write_text("v1\n")
            (cell_dir / f"{name}.spice").write_text(f".subckt {name} a\n.ends\n")
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
        # nand2 is "broken": the fake netgen reports a mismatch for it alone.
        _write_executable(self.bindir / "netgen", """#!/bin/sh
if echo "$*" | grep -q nand2; then
  echo "Circuits do not match"
else
  echo "Circuits match uniquely."
fi
""")
        self.old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.bindir}{os.pathsep}{self.old_path}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", self.old_path))

    def _run(self, **overrides):
        kwargs = dict(
            targets=(self.repo / "cells" / "inv", self.repo / "cells" / "nand2"),
            out_root=self.repo / "out", pdk_root=str(self.pdk_root), pdk_variant=None,
            pdk_commit="a" * 40, dry_run=False, disable_aslr_guard=True, use_cache=True,
            fail_fast=False,
        )
        kwargs.update(overrides)
        return run_batch(**kwargs)

    def test_overall_exit_code_is_the_worst_of_the_individual_cells(self):
        outcome = self._run()
        self.assertEqual(outcome.exit_code, exitcodes.NOT_CLEAN)
        by_label = dict(outcome.results)
        self.assertEqual(by_label["inv"].exit_code, exitcodes.OK)
        self.assertEqual(by_label["nand2"].exit_code, exitcodes.NOT_CLEAN)

    def test_each_cell_gets_its_own_output_subdirectory_and_manifest(self):
        self._run()
        self.assertTrue((self.repo / "out" / "inv" / "manifest.json").is_file())
        self.assertTrue((self.repo / "out" / "nand2" / "manifest.json").is_file())

    def test_fail_fast_stops_after_the_first_non_ok_cell(self):
        # Reversed order so the broken cell (nand2) is checked first.
        outcome = self._run(
            targets=(self.repo / "cells" / "nand2", self.repo / "cells" / "inv"),
            fail_fast=True,
        )
        self.assertEqual(len(outcome.results), 1)
        self.assertEqual(outcome.results[0][0], "nand2")

    def test_batch_reuses_run_check_outcomes(self):
        outcome = self._run()
        by_label = dict(outcome.results)
        self.assertEqual(by_label["inv"].manifest["verification"]["lvs_verdict"], "pass")
        self.assertEqual(by_label["nand2"].manifest["verification"]["lvs_verdict"], "fail")


if __name__ == "__main__":
    unittest.main()
