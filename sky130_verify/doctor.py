"""Environment diagnostics for ``sky130-verify doctor`` and ``check``.

Performs executable, version, and file-presence probes.
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
    commit_verified: bool = True


@dataclass(frozen=True)
class KlayoutTechStatus:
    """KLayout technology resolution for --with-klayout."""

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
        """Ready for ``check``: required tools found, PDK resolved with a
        verified commit. ``setarch`` availability doesn't factor in."""
        required_ok = all(t.found for t in self.tools if t.required)
        return required_ok and self.pdk.found and self.pdk.commit_sha is not None and self.pdk.commit_verified

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
                "commit_verified": self.pdk.commit_verified,
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


# Netgen has no version flag; `-batch exit` just captures its startup
# banner (see _BEST_EFFORT_VERSION_TOOLS below).
_VERSION_PROBES: dict[str, tuple[str, ...]] = {
    "magic": ("magic", "--version"),
    "netgen": ("netgen", "-batch", "exit"),
    "klayout": ("klayout", "-v"),
}

_BEST_EFFORT_VERSION_TOOLS = frozenset({"netgen"})

_REQUIRED_TOOLS = ("magic", "netgen")

_REMEDIATION = {
    "magic": "build from https://github.com/RTimothyEdwards/magic (see its README/INSTALL)",
    "netgen": ("build from https://github.com/RTimothyEdwards/netgen (see its README), "
               "or the `netgen-lvs` system package on Debian/Ubuntu"),
    "klayout": "official packages: https://www.klayout.de/build.html",
    "pdk": ("manage the PDK version with ciel (https://github.com/fossi-foundation/ciel) "
            "or volare (https://github.com/efabless/volare), then export PDK_ROOT, "
            "or pass --pdk-root explicitly"),
    "klayout_tech": ("clone https://github.com/efabless/sky130_klayout_pdk at a fixed commit, "
                      "then export KLAYOUT_TECH_PATH=<clone>/tech/sky130, "
                      "or pass --klayout-tech explicitly"),
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
    """Resolve an installed Sky130A PDK.

    Precedence: ``--pdk-root`` then ``$PDK_ROOT``.
    """
    if not pdk_root:
        pdk_root = os.environ.get("PDK_ROOT")
    if not pdk_root:
        return PdkStatus(root=None, variant=variant, magicrc=None, netgen_setup=None,
                          found=False, commit_sha=None, commit_source="unavailable",
                          commit_verified=False,
                          note="PDK_ROOT not set (neither --pdk-root nor the environment variable)")

    root = Path(pdk_root)
    magicrc = root / variant / "libs.tech" / "magic" / f"{variant}.magicrc"
    netgen_setup = root / variant / "libs.tech" / "netgen" / f"{variant}_setup.tcl"
    found = magicrc.is_file() and netgen_setup.is_file()

    detected_commit = gitinfo.head(root) or gitinfo.head(root.parent)
    if explicit_commit is not None:
        commit_sha = explicit_commit
        commit_source = "explicit"
        commit_verified = detected_commit is None or detected_commit == explicit_commit
    elif detected_commit is not None:
        commit_sha = detected_commit
        commit_source = "git"
        commit_verified = True
    else:
        commit_sha = None
        commit_source = "unavailable"
        commit_verified = False

    note = "resolved" if found else (
        f"expected open_pdks layout not found under {root / variant}"
    )
    if found and commit_sha is None:
        note += " ; commit not determined automatically — supply --pdk-commit"
    elif found and not commit_verified:
        note += (
            " ; --pdk-commit does not match the clone's detected HEAD "
            f"({detected_commit}) — fix the SHA or use the matching PDK"
        )

    return PdkStatus(root=str(root), variant=variant, magicrc=str(magicrc) if found else None,
                      netgen_setup=str(netgen_setup) if found else None, found=found,
                      commit_sha=commit_sha, commit_source=commit_source,
                      commit_verified=commit_verified, note=note)


def resolve_klayout_tech(klayout_tech: str | None) -> KlayoutTechStatus:
    """Resolve the KLayout technology root and its DRC deck."""
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
    note = "resolved" if found else f"expected DRC deck not found under {deck_path}"
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
    lines = [f"platform       : {report.platform_system} {report.platform_machine} (python {report.python_version})"]
    for t in report.tools:
        marker = "ok" if t.found else ("MISSING" if t.required else "absent (optional)")
        detail = t.version or t.path or ""
        if detail and t.version_is_best_effort:
            detail += " (banner only, no version guarantee — netgen has no version flag)"
        lines.append(f"tool {t.name:<9}: {marker}" + (f" — {detail}" if detail else ""))
        if not t.found and t.name in _REMEDIATION:
            lines.append(f"  -> {_REMEDIATION[t.name]}")
    lines.append(f"ASLR guard     : {'available (setarch)' if report.aslr_guard_available else 'unavailable — degraded, non-blocking'}")
    p = report.pdk
    lines.append(f"PDK ({p.variant})   : {'resolved' if p.found else 'NOT RESOLVED'} — {p.note}")
    if not p.found:
        lines.append(f"  -> {_REMEDIATION['pdk']}")
    if report.klayout_tech is not None:
        kt = report.klayout_tech
        lines.append(f"KLayout tech   : {'resolved (optional)' if kt.found else 'not resolved (optional, --with-klayout)'} — {kt.note}")
        if not kt.found:
            lines.append(f"  -> {_REMEDIATION['klayout_tech']}")
    lines.append("")
    lines.append("ready for `sky130-verify check`: " + ("yes" if report.ok else "no"))
    return "\n".join(lines)
