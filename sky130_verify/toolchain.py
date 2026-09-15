"""Run Magic DRC and extraction, Netgen LVS, and KLayout DRC."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import platform
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET

TCL_DIR = Path(__file__).resolve().parent / "tcl"


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    log_path: Path


@dataclass(frozen=True)
class DrcResult:
    ok: bool
    error_count: int | None
    verdict: str  # "pass" | "fail" | "unknown" | "error"
    command: CommandResult


@dataclass(frozen=True)
class ExtractResult:
    ok: bool
    spice_path: Path | None
    command: CommandResult


@dataclass(frozen=True)
class LvsResult:
    verdict: str  # "pass" | "fail" | "unknown" | "error"
    command: CommandResult
    comparison_path: Path | None = None


@dataclass(frozen=True)
class GdsWriteResult:
    ok: bool
    gds_path: Path | None
    command: CommandResult


@dataclass(frozen=True)
class KlayoutDrcResult:
    verdict: str  # "pass" | "fail" | "error"
    violation_count: int | None
    command: CommandResult
    report_path: Path | None = None


def aslr_guard_available() -> bool:
    """``setarch`` is on the PATH AND its ``personality()`` call actually
    works here (Docker's default seccomp profile blocks the latter)."""
    setarch = shutil.which("setarch")
    if not setarch:
        return False
    try:
        probe = subprocess.run(
            [setarch, platform.machine(), "-R", "--", "true"],
            capture_output=True, timeout=5,
        )
    except Exception:
        return False
    return probe.returncode == 0


def aslr_guard_command(argv: list[str], *, disabled: bool = False) -> list[str]:
    """Prefix ``argv`` with ``setarch <arch> -R`` when available."""
    if disabled or not aslr_guard_available():
        return argv
    return [shutil.which("setarch"), platform.machine(), "-R", "--", *argv]


def _base_env(*, pdk_root: Path, pdk_variant: str, work_dir: Path) -> dict[str, str]:
    """Return the environment shared by EDA tool invocations."""
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", str(work_dir)),
        "PDK_ROOT": str(pdk_root),
        "PDK": pdk_variant,
    }


def _run(argv: list[str], *, cwd: Path, env: dict, log_path: Path, timeout: int) -> CommandResult:
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.run(argv, cwd=str(cwd), env=env, stdout=log, stderr=subprocess.STDOUT,
                               timeout=timeout)
    return CommandResult(argv=tuple(argv), returncode=proc.returncode, log_path=log_path)


def _was_signal_killed(returncode: int) -> bool:
    """``True`` if the subprocess was killed by a signal (negative
    returncode). A positive nonzero code isn't treated as a crash: Netgen
    has no documented exit code for pass/fail."""
    return returncode < 0


_DRC_COUNT_RE = re.compile(r"Total DRC errors found:\s*(\d+)")


def run_drc(*, view: Path, cell_format: str, cell: str, magicrc: Path, pdk_root: Path,
            pdk_variant: str, work_dir: Path, disable_aslr_guard: bool = False,
            timeout: int = 600) -> DrcResult:
    """Geometric DRC of a cell via ``magic -dnull -noconsole -rcfile
    <magicrc> tcl/drc.tcl``. Error count parsed from the "Total DRC errors
    found: N" line (its Tcl return value isn't reliable)."""
    argv = aslr_guard_command(
        ["magic", "-dnull", "-noconsole", "-rcfile", str(magicrc), str(TCL_DIR / "drc.tcl")],
        disabled=disable_aslr_guard,
    )
    env = _base_env(pdk_root=pdk_root, pdk_variant=pdk_variant, work_dir=work_dir) | {
        "SKY130VERIFY_VIEW": str(view),
        "SKY130VERIFY_FORMAT": cell_format,
        "SKY130VERIFY_CELL": cell,
    }
    log_path = work_dir / "drc.log"
    command = _run(argv, cwd=work_dir, env=env, log_path=log_path, timeout=timeout)
    if _was_signal_killed(command.returncode):
        return DrcResult(ok=False, error_count=None, verdict="error", command=command)
    text = log_path.read_text(encoding="utf-8", errors="replace")
    loaded = "DRC_LOAD_OK" in text
    ran = "DRC_RUN_OK" in text
    if not loaded or not ran:
        return DrcResult(ok=False, error_count=None, verdict="error", command=command)
    m = _DRC_COUNT_RE.search(text)
    count = int(m.group(1)) if m else None
    verdict = "unknown" if count is None else ("pass" if count == 0 else "fail")
    return DrcResult(ok=True, error_count=count, verdict=verdict, command=command)


def run_extraction(*, view: Path, cell_format: str, cell: str, magicrc: Path, pdk_root: Path,
                    pdk_variant: str, work_dir: Path, disable_aslr_guard: bool = False,
                    timeout: int = 600) -> ExtractResult:
    """Layout -> SPICE extraction. For the ``mag`` format, the caller must
    already have copied the view to ``work_dir/<cell>.mag`` — Magic's
    ``load $cell`` loads by name, not by path, and would otherwise silently
    pick up a same-named PDK cell instead."""
    argv = aslr_guard_command(
        ["magic", "-dnull", "-noconsole", "-rcfile", str(magicrc), str(TCL_DIR / "extract.tcl")],
        disabled=disable_aslr_guard,
    )
    env = _base_env(pdk_root=pdk_root, pdk_variant=pdk_variant, work_dir=work_dir) | {
        "SKY130VERIFY_VIEW": str(view),
        "SKY130VERIFY_FORMAT": cell_format,
        "SKY130VERIFY_CELL": cell,
    }
    log_path = work_dir / "extract.log"
    command = _run(argv, cwd=work_dir, env=env, log_path=log_path, timeout=timeout)
    if _was_signal_killed(command.returncode):
        return ExtractResult(ok=False, spice_path=None, command=command)
    text = log_path.read_text(encoding="utf-8", errors="replace")
    ok = "EXTRACT_LOAD_OK" in text and "EXTRACT_RUN_OK" in text
    spice_path = None
    if ok:
        for candidate in (work_dir / f"{cell}.spice", work_dir / f"{cell}.spc"):
            if candidate.is_file() and candidate.stat().st_size > 0:
                spice_path = candidate
                break
    return ExtractResult(ok=ok and spice_path is not None, spice_path=spice_path, command=command)


_MATCH_RE = re.compile(r"Circuits match uniquely")
_PROPERTY_ERROR_RE = re.compile(r"property errors")
_MISMATCH_RE = re.compile(r"failed pin matching|Circuits do not match|Netlists do not match|property errors")


def parse_lvs_log(text: str) -> str:
    """Parses a Netgen LVS log into a verdict."""
    if _MATCH_RE.search(text) and not _PROPERTY_ERROR_RE.search(text):
        return "pass"
    if _MISMATCH_RE.search(text):
        return "fail"
    return "unknown"


def run_lvs(*, extracted_spice: Path, golden_spice: Path, cell: str, netgen_setup: Path,
            pdk_root: Path, pdk_variant: str, work_dir: Path, log_name: str = "lvs.log",
            timeout: int = 600) -> LvsResult:
    """Netgen LVS via ``netgen -batch lvs "<extracted> <cell>" "<golden>
    <cell>" <setup.tcl> <output.out>``."""
    comp_out = work_dir / (log_name.removesuffix(".log") + ".out")
    argv = [
        "netgen", "-batch", "lvs",
        f"{extracted_spice} {cell}",
        f"{golden_spice} {cell}",
        str(netgen_setup),
        str(comp_out),
    ]
    env = _base_env(pdk_root=pdk_root, pdk_variant=pdk_variant, work_dir=work_dir)
    log_path = work_dir / log_name
    command = _run(argv, cwd=work_dir, env=env, log_path=log_path, timeout=timeout)
    if _was_signal_killed(command.returncode):
        return LvsResult(verdict="error", command=command, comparison_path=None)
    text = log_path.read_text(encoding="utf-8", errors="replace")
    # netgen's device/pin correspondence, not repeated in the main log.
    comparison_path = comp_out if comp_out.is_file() else None
    return LvsResult(verdict=parse_lvs_log(text), command=command, comparison_path=comparison_path)


def run_gds_write(*, view: Path, cell: str, magicrc: Path, pdk_root: Path, pdk_variant: str,
                   work_dir: Path, disable_aslr_guard: bool = False, timeout: int = 600) -> GdsWriteResult:
    """Convert a ``.mag`` cell to ``.gds`` for the KLayout cross-check."""
    argv = aslr_guard_command(
        ["magic", "-dnull", "-noconsole", "-rcfile", str(magicrc), str(TCL_DIR / "gdswrite.tcl")],
        disabled=disable_aslr_guard,
    )
    env = _base_env(pdk_root=pdk_root, pdk_variant=pdk_variant, work_dir=work_dir) | {
        "SKY130VERIFY_VIEW": str(view),
        "SKY130VERIFY_CELL": cell,
    }
    log_path = work_dir / "gdswrite.log"
    command = _run(argv, cwd=work_dir, env=env, log_path=log_path, timeout=timeout)
    if _was_signal_killed(command.returncode):
        return GdsWriteResult(ok=False, gds_path=None, command=command)
    text = log_path.read_text(encoding="utf-8", errors="replace")
    ok = "GDSWRITE_LOAD_OK" in text and "GDSWRITE_OK" in text
    gds_path = work_dir / "converted.gds"
    ok = ok and gds_path.is_file() and gds_path.stat().st_size > 0
    return GdsWriteResult(ok=ok, gds_path=gds_path if ok else None, command=command)


def _count_rdb_violations(report_path: Path) -> int | None:
    """Count ``<item>`` elements in a KLayout XML report database."""
    try:
        root = ET.parse(report_path).getroot()
    except (ET.ParseError, OSError):
        return None
    items = root.find("items")
    if items is None:
        return None
    return len(items.findall("item"))


def run_klayout_drc(*, gds: Path, cell: str, deck: Path, work_dir: Path,
                     timeout: int = 600) -> KlayoutDrcResult:
    """Run a KLayout DRC cross-check.

    ``sky130A_mr.drc`` reads the ``top_cell`` variable.
    """
    report_path = work_dir / "klayout-drc-report.xml"
    argv = ["klayout", "-b", "-r", str(deck), "-rd", f"input={gds}",
            "-rd", f"report={report_path}", "-rd", f"top_cell={cell}"]
    log_path = work_dir / "klayout-drc.log"
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", str(work_dir))}
    command = _run(argv, cwd=work_dir, env=env, log_path=log_path, timeout=timeout)
    if _was_signal_killed(command.returncode):
        return KlayoutDrcResult(verdict="error", violation_count=None, command=command)
    count = _count_rdb_violations(report_path)
    if count is None:
        return KlayoutDrcResult(verdict="error", violation_count=None, command=command)
    return KlayoutDrcResult(verdict="pass" if count == 0 else "fail", violation_count=count,
                             command=command, report_path=report_path)
