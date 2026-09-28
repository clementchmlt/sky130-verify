"""Run Magic, Netgen and KLayout and parse their output."""

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
    # Undefined subcircuits that are not PDK setup primitives: their content
    # was not compared (see non_device_placeholders).
    black_boxes: tuple[str, ...] = ()


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
    """``setarch`` is on the PATH and ``setarch <arch> -R`` actually works.

    Both conditions are needed: Docker's default seccomp profile keeps
    ``setarch`` on the PATH but rejects ``personality()`` with ``Operation not
    permitted``, which would make every Magic invocation fail.
    """
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
    """Prefix ``argv`` with ``setarch <arch> -R`` unless disabled or unavailable.

    Magic writes its extraction files in an order that follows memory
    addresses (Magic issues #304 and #551); with ASLR disabled they are
    byte-identical between runs. The netlist and the verdict are not
    affected, so the guard is applied when possible but never required.
    The exact command line, with or without the prefix, is recorded in
    ``run.json``.
    """
    if disabled or not aslr_guard_available():
        return argv
    return [shutil.which("setarch"), platform.machine(), "-R", "--", *argv]


def _base_env(*, pdk_root: Path, pdk_variant: str, work_dir: Path) -> dict[str, str]:
    """Minimal explicit environment for Magic and Netgen.

    The inherited environment is never passed through: a stray ``PDK_ROOT``
    or ``PDK`` exported for another tool would silently select another PDK
    tree.
    """
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
    """``True`` when the subprocess was killed by a signal (negative
    returncode).

    A process killed after printing its success markers must not count as a
    clean run. A positive non-zero returncode is not treated as a crash:
    Netgen has no documented exit code distinguishing pass from fail.
    """
    return returncode < 0


_DRC_COUNT_RE = re.compile(r"Total DRC errors found:\s*(\d+)")


def run_drc(*, view: Path, cell_format: str, cell: str, magicrc: Path, pdk_root: Path,
            pdk_variant: str, work_dir: Path, disable_aslr_guard: bool = False,
            timeout: int = 600) -> DrcResult:
    """Magic DRC of one cell: ``magic -dnull -noconsole -rcfile <magicrc>
    tcl/drc.tcl``. The error count is read from the ``Total DRC errors
    found: N`` line printed by ``drc count total``; its Tcl return value is
    not reliable.
    """
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
    """Magic extraction from layout to SPICE.

    For ``.mag`` input, Magic's ``load <cell>`` ignores the given path and
    searches the working directory, then the PDK library path: a cell named
    like a PDK standard cell would silently load the PDK copy. The caller
    must therefore copy the view to ``work_dir/<cell>.mag`` first and pass
    that copy as ``view``.
    """
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


_UNDEFINED_SUBCIRCUIT_RE = re.compile(r"Call to undefined subcircuit (\S+)")
_SETUP_DEVICE_RE = re.compile(r"^\s*lappend\s+devices\s+(.+?)\s*$", re.M)

# Include the log parser version in the LVS cache key.
LVS_RULE_VERSION = "2"


def netgen_setup_devices(setup_text: str) -> frozenset[str]:
    """Devices declared by the PDK's Netgen setup (``lappend devices``).

    The setup gives these primitives property comparison rules (W and L
    tolerances, series/parallel merging). Netgen reports them as "Call to
    undefined subcircuit" and creates placeholders, yet still compares their
    properties: doubling W or L of a reference transistor yields "Property
    errors".
    """
    names: set[str] = set()
    for line in _SETUP_DEVICE_RE.findall(setup_text):
        names.update(line.split())
    return frozenset(names)


def undefined_subcircuits(text: str) -> tuple[str, ...]:
    """Subcircuits Netgen left undefined, in log order."""
    return tuple(dict.fromkeys(_UNDEFINED_SUBCIRCUIT_RE.findall(text)))


def non_device_placeholders(text: str, devices: frozenset[str]) -> tuple[str, ...]:
    """Undefined subcircuits that are not PDK setup primitives.

    An undefined primitive is still compared by its properties. Any other
    undefined subcircuit (an author block missing from the netlist, a
    standard cell not provided) is compared as a black box: its content is
    never checked, and "Circuits match uniquely" says nothing about it.
    """
    return tuple(name for name in undefined_subcircuits(text) if name not in devices)


def parse_lvs_log(text: str, devices: frozenset[str] | None = None) -> str:
    """Return the LVS verdict; unresolved non-PDK subcircuits are inconclusive."""
    if _MATCH_RE.search(text) and not _PROPERTY_ERROR_RE.search(text):
        if devices is not None and non_device_placeholders(text, devices):
            return "unknown"
        return "pass"
    if _MISMATCH_RE.search(text):
        return "fail"
    return "unknown"


def run_lvs(*, extracted_spice: Path, golden_spice: Path, cell: str, netgen_setup: Path,
            pdk_root: Path, pdk_variant: str, work_dir: Path, log_name: str = "lvs.log",
            timeout: int = 600) -> LvsResult:
    """Netgen LVS: ``netgen -batch lvs "<extracted> <cell>" "<reference>
    <cell>" <setup.tcl> <output.out>``. DRC and LVS are separate invocations
    with separate verdicts.
    """
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
    # Netgen writes the detailed device/pin matching to comp_out, not to the
    # main log; check.py adds it to the manifest's hashed artifacts.
    comparison_path = comp_out if comp_out.is_file() else None
    # An unreadable setup declares no primitive, so every undefined
    # subcircuit makes the result inconclusive: the safe direction.
    try:
        devices = netgen_setup_devices(netgen_setup.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        devices = frozenset()
    return LvsResult(verdict=parse_lvs_log(text, devices), command=command, comparison_path=comparison_path,
                     black_boxes=non_device_placeholders(text, devices))


def run_gds_write(*, view: Path, cell: str, magicrc: Path, pdk_root: Path, pdk_variant: str,
                   work_dir: Path, disable_aslr_guard: bool = False, timeout: int = 600) -> GdsWriteResult:
    """Convert .mag to .gds for the KLayout cross-check (--with-klayout),
    since KLayout does not read .mag. Not called for .gds input.
    """
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
    """Count the ``<item>`` entries of a KLayout report database (XML).

    A missing or unreadable report returns ``None``, never zero violations.
    """
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
    """KLayout DRC cross-check, reported separately from the Magic DRC
    verdict: ``klayout -b -r <deck> -rd input=<gds> -rd report=<report.xml>
    -rd top_cell=<cell>``. The ``sky130A_mr.drc`` deck reads ``top_cell``,
    not ``topcell``.
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
