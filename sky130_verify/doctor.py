"""Environment diagnosis for ``sky130-verify doctor``, also run before ``check``.

Runs no verification: only version probes and file-presence checks, never
DRC or LVS.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import platform
import shutil
import subprocess
from pathlib import Path

from . import gitinfo, toolchain


@dataclass(frozen=True)
class ToolStatus:
    name: str
    found: bool
    path: str | None
    version: str | None
    required: bool
    version_is_best_effort: bool = False


@dataclass(frozen=True)
class PdkStatus:
    root: str | None
    variant: str
    magicrc: str | None
    netgen_setup: str | None
    found: bool
    commit_sha: str | None
    note: str
    commit_source: str = "unknown"
    commit_consistent: bool = True


@dataclass(frozen=True)
class KlayoutTechStatus:
    """KLayout technology used by --with-klayout; never required otherwise."""

    root: str | None
    deck_path: str | None
    found: bool
    note: str


@dataclass(frozen=True)
class DoctorReport:
    platform_machine: str
    platform_system: str
    python_version: str
    tools: tuple[ToolStatus, ...]
    pdk: PdkStatus
    aslr_guard_available: bool
    klayout_tech: KlayoutTechStatus | None = None

    @property
    def ok(self) -> bool:
        """Report whether required tools, PDK files, and commit are available."""
        required_ok = all(t.found for t in self.tools if t.required)
        # Without a known, consistent PDK commit the manifest cannot be
        # replayed; refuse before running Magic and Netgen.
        return required_ok and self.pdk.found and self.pdk.commit_sha is not None and self.pdk.commit_consistent

    def to_dict(self) -> dict:
        return {
            "platform": {
                "machine": self.platform_machine,
                "system": self.platform_system,
                "python": self.python_version,
            },
            "tools": [
                {
                    "name": t.name,
                    "found": t.found,
                    "path": t.path,
                    "version": t.version,
                    "version_is_best_effort": t.version_is_best_effort,
                    "required": t.required,
                    "hint": None if t.found else _REMEDIATION.get(t.name),
                }
                for t in self.tools
            ],
            "pdk": {
                "root": self.pdk.root,
                "variant": self.pdk.variant,
                "magicrc": self.pdk.magicrc,
                "netgen_setup": self.pdk.netgen_setup,
                "found": self.pdk.found,
                "commit_sha": self.pdk.commit_sha,
                "commit_source": self.pdk.commit_source,
                "commit_consistent": self.pdk.commit_consistent,
                "note": self.pdk.note,
                "hint": None if self.pdk.found else _REMEDIATION["pdk"],
            },
            "aslr_guard_available": self.aslr_guard_available,
            "ready_for_check": self.ok,
            "klayout_tech": (
                None if self.klayout_tech is None else {
                    "root": self.klayout_tech.root,
                    "deck_path": self.klayout_tech.deck_path,
                    "found": self.klayout_tech.found,
                    "note": self.klayout_tech.note,
                    "hint": None if self.klayout_tech.found else _REMEDIATION["klayout_tech"],
                }
            ),
        }


# Version probes:
#
# - `magic --version` prints the version and exits without a display
#   (opencircuitdesign.com/magic/userguide.html).
# - Netgen has no version flag (`netgen.sh.in` accepts only -noc*/-bat*/-gui).
#   `-batch exit` captures the startup banner, which doctor labels as such.
# - `klayout -v` prints the program version and exits
#   (klayout.de/command_args.html).
_VERSION_PROBES: dict[str, tuple[str, ...]] = {
    "magic": ("magic", "--version"),
    "netgen": ("netgen", "-batch", "exit"),
    "klayout": ("klayout", "-v"),
}

# Tools without a version flag: their `version` is a startup banner.
_BEST_EFFORT_VERSION_TOOLS = frozenset({"netgen"})

_REQUIRED_TOOLS = ("magic", "netgen")

RESOLVED = "resolved"

# Where to get each missing component (official repositories only).
_REMEDIATION = {
    "magic": "build from https://github.com/RTimothyEdwards/magic (see its README and INSTALL)",
    "netgen": ("build from https://github.com/RTimothyEdwards/netgen (see its README), "
               "or install the `netgen-lvs` package on Debian/Ubuntu"),
    "klayout": "official packages: https://www.klayout.de/build.html",
    "pdk": ("install the PDK with ciel (https://github.com/fossi-foundation/ciel) "
            "or volare (https://github.com/efabless/volare), then export PDK_ROOT "
            "or pass --pdk-root"),
    "klayout_tech": ("clone https://github.com/efabless/sky130_klayout_pdk at a fixed commit, "
                      "then export KLAYOUT_TECH_PATH=<clone>/tech/sky130 "
                      "or pass --klayout-tech"),
}


def _tool_version(cmd: tuple[str, ...]) -> str | None:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except Exception:
        return None
    text = (out.stdout or out.stderr or "").strip()
    return text.splitlines()[0] if text else None


def probe_tool(name: str) -> ToolStatus:
    probe = _VERSION_PROBES.get(name, (name, "--version"))
    path = shutil.which(name)
    version = _tool_version(probe) if path else None
    return ToolStatus(name=name, found=path is not None, path=path, version=version,
                       required=name in _REQUIRED_TOOLS,
                       version_is_best_effort=name in _BEST_EFFORT_VERSION_TOOLS)


def resolve_pdk(pdk_root: str | None, variant: str, explicit_commit: str | None = None) -> PdkStatus:
    """Resolve PDK files and a supplied or directly detectable Git commit."""
    if not pdk_root:
        pdk_root = os.environ.get("PDK_ROOT")
    if not pdk_root:
        return PdkStatus(root=None, variant=variant, magicrc=None, netgen_setup=None,
                          found=False, commit_sha=None, commit_source="unavailable",
                          commit_consistent=False,
                          note="PDK_ROOT not set (neither --pdk-root nor the environment variable)")

    root = Path(pdk_root)
    magicrc = root / variant / "libs.tech" / "magic" / f"{variant}.magicrc"
    netgen_setup = root / variant / "libs.tech" / "netgen" / f"{variant}_setup.tcl"
    found = magicrc.is_file() and netgen_setup.is_file()

    repo_root = gitinfo.toplevel(root)
    detected_commit = (
        gitinfo.head(repo_root)
        if repo_root is not None and repo_root.resolve() in (root.resolve(), root.parent.resolve())
        else None
    )
    if explicit_commit is not None:
        commit_sha = explicit_commit
        commit_source = "explicit"
        commit_consistent = detected_commit is None or detected_commit == explicit_commit
    elif detected_commit is not None:
        commit_sha = detected_commit
        commit_source = "git"
        commit_consistent = True
    else:
        commit_sha = None
        commit_source = "unavailable"
        commit_consistent = False

    missing_files = [str(path) for path in (magicrc, netgen_setup) if not path.is_file()]
    note = RESOLVED if found else f"missing PDK file(s): {', '.join(missing_files)}"
    if found and commit_sha is None:
        note = "PDK files found but the PDK commit is unknown; pass --pdk-commit"
    elif found and not commit_consistent:
        note = (
            f"--pdk-commit {explicit_commit} does not match PDK checkout HEAD "
            f"{detected_commit}; pass the matching SHA or use that PDK checkout"
        )

    return PdkStatus(root=str(root), variant=variant, magicrc=str(magicrc) if found else None,
                      netgen_setup=str(netgen_setup) if found else None, found=found,
                      commit_sha=commit_sha, commit_source=commit_source,
                      commit_consistent=commit_consistent, note=note)


def resolve_klayout_tech(klayout_tech: str | None) -> KlayoutTechStatus:
    """Resolve the KLayout technology root (efabless/sky130_klayout_pdk,
    expected layout ``<root>/drc/sky130A_mr.drc``); never cloned here.
    ``--klayout-tech`` takes precedence over ``$KLAYOUT_TECH_PATH``."""
    if not klayout_tech:
        klayout_tech = os.environ.get("KLAYOUT_TECH_PATH")
    if not klayout_tech:
        return KlayoutTechStatus(
            root=None, deck_path=None, found=False,
            note="KLAYOUT_TECH_PATH not set (neither --klayout-tech nor the environment variable)",
        )
    root = Path(klayout_tech)
    deck_path = root / "drc" / "sky130A_mr.drc"
    found = deck_path.is_file()
    note = RESOLVED if found else f"expected DRC deck not found: {deck_path}"
    return KlayoutTechStatus(root=str(root), deck_path=str(deck_path) if found else None,
                              found=found, note=note)


def run_doctor(pdk_root: str | None, variant: str = "sky130A",
               explicit_pdk_commit: str | None = None,
               klayout_tech: str | None = None) -> DoctorReport:
    tools = tuple(probe_tool(name) for name in ("magic", "netgen", "klayout"))
    pdk = resolve_pdk(pdk_root, variant, explicit_pdk_commit)
    aslr_guard = toolchain.aslr_guard_available()
    return DoctorReport(
        platform_machine=platform.machine(),
        platform_system=platform.system(),
        python_version=platform.python_version(),
        tools=tools,
        pdk=pdk,
        aslr_guard_available=aslr_guard,
        klayout_tech=resolve_klayout_tech(klayout_tech),
    )


def format_report_human(report: DoctorReport) -> str:
    def row(label: str, value: str) -> str:
        return f"{label:<14}: {value}"

    lines = [row("platform", f"{report.platform_system} {report.platform_machine} "
                             f"(python {report.python_version})")]
    for t in report.tools:
        marker = "ok" if t.found else ("MISSING" if t.required else "not found (optional)")
        detail = t.version or t.path or ""
        if detail and t.version_is_best_effort:
            detail += " (startup banner; netgen has no version flag)"
        lines.append(row(t.name, marker + (f": {detail}" if detail else "")))
        if not t.found and t.name in _REMEDIATION:
            lines.append(f"  -> {_REMEDIATION[t.name]}")
    lines.append(row("ASLR guard", "available (setarch -R)" if report.aslr_guard_available
                     else "unavailable (not blocking; Magic runs with ASLR enabled)"))
    p = report.pdk
    if not p.found:
        pdk_state = f"NOT RESOLVED: {p.note}"
    elif p.note != RESOLVED:
        pdk_state = f"INCOMPLETE: {p.note}"
    else:
        pdk_state = f"resolved, commit {p.commit_sha} ({p.commit_source})"
    lines.append(row(f"PDK {p.variant}", pdk_state))
    if not p.found:
        lines.append(f"  -> {_REMEDIATION['pdk']}")
    if report.klayout_tech is not None:
        kt = report.klayout_tech
        kt_state = ("resolved (optional)" if kt.found
                    else f"not resolved (optional, for --with-klayout): {kt.note}")
        lines.append(row("KLayout tech", kt_state))
        if not kt.found:
            lines.append(f"  -> {_REMEDIATION['klayout_tech']}")
    lines.append("")
    lines.append("ready for `sky130-verify check`: " + ("yes" if report.ok else "no"))
    return "\n".join(lines)
