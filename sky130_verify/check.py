"""``sky130-verify check``: one cell, one manifest.

Pipeline: resolve the environment, DRC, extraction, LVS, eight-field
manifest, badge. A missing required tool stops the pipeline with exit code 3.
Arguments are validated before any Magic or Netgen invocation, so a malformed
``--pdk-commit`` is rejected before a full DRC and LVS run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from . import badge, doctor, exitcodes, gitinfo, toolchain
from .manifest_backend import ManifestValidationError, Pdk, Source, Verification, VerificationManifest
from .verify_config import VerifyConfig, VerifyConfigError, find_verify_config, load_verify_config

_LAYOUT_SUFFIXES = (".mag", ".gds")
_SCHEMATIC_SUFFIXES = (".spice", ".spc", ".cdl", ".sp")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
_CELL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_SCP_LIKE_GIT_URL_RE = re.compile(r"^(?P<user>[^@\s/]+)@(?P<host>[^:\s/]+):(?P<path>.+)$")


def normalize_repository_url(value: str) -> str:
    """Convert an SCP-like git URL (``git@host:path``) to an ``ssh://`` URI.

    The manifest requires a URL with a scheme; SSH remotes such as
    ``git@github.com:org/repo.git`` are common. Any other value is returned
    unchanged.
    """
    if "://" in value:
        return value
    m = _SCP_LIKE_GIT_URL_RE.match(value)
    if m:
        return f"ssh://{m.group('user')}@{m.group('host')}/{m.group('path')}"
    return value


class UsageError(Exception):
    """Invocation error: exit code 2."""


def validate_git_sha(value: str | None, field_name: str) -> None:
    if value is not None and not _GIT_SHA_RE.fullmatch(value):
        raise UsageError(
            f"{field_name} must be a full git SHA (40 to 64 hexadecimal characters), "
            f"got {value!r}"
        )


def validate_repository_url(value: str | None, field_name: str) -> None:
    if value is not None and "://" not in value:
        raise UsageError(f"{field_name} must be a repository URL containing '://', got {value!r}")


def validate_cell_name(value: str, field_name: str) -> None:
    if not _CELL_NAME_RE.fullmatch(value):
        raise UsageError(
            f"{field_name} must start with a letter or underscore and contain only letters, "
            f"digits, '_', '.' or '-'; {value!r} cannot be passed safely to Magic or Netgen"
        )


@dataclass(frozen=True)
class CellInputs:
    layout: Path
    cell_format: str  # "mag" | "gds"
    cell: str
    schematic: Path


def _pick_single(directory: Path, suffixes: tuple[str, ...], role: str) -> Path:
    candidates = sorted(p for p in directory.iterdir() if p.suffix.lower() in suffixes and p.is_file())
    if not candidates:
        raise UsageError(
            f"no {role} file ({'/'.join(suffixes)}) found in {directory}; "
            "add the file or select it in verify.toml"
        )
    if len(candidates) > 1:
        names = ", ".join(c.name for c in candidates)
        raise UsageError(
            f"several {role} files in {directory} ({names}); "
            "name the file explicitly or add a verify.toml"
        )
    return candidates[0]


def resolve_cell_inputs(target: Path, *, cell_override: str | None,
                         schematic_override: Path | None,
                         layout_override: Path | None = None) -> CellInputs:
    if target.is_dir():
        layout = layout_override or _pick_single(target, _LAYOUT_SUFFIXES, "layout")
        schematic = schematic_override or _pick_single(target, _SCHEMATIC_SUFFIXES, "reference schematic")
    elif target.is_file():
        layout = target
        if schematic_override is None:
            raise UsageError("a layout file target requires --schematic")
        schematic = schematic_override
    else:
        raise UsageError(f"cell path not found: {target}; pass an existing cell directory or layout file")

    cell_format = layout.suffix.lower().lstrip(".")
    if cell_format not in ("mag", "gds"):
        raise UsageError(f"unsupported layout format: {layout.suffix} (expected .mag or .gds)")
    if not schematic.is_file():
        raise UsageError(f"reference schematic not found: {schematic}; pass an existing --schematic file")
    cell = cell_override or layout.stem
    validate_cell_name(cell, "the cell name (--cell, or the layout file name)")
    # Absolute paths: Netgen runs with cwd=work_dir, where a relative
    # schematic path would not resolve.
    return CellInputs(layout=layout.resolve(), cell_format=cell_format, cell=cell,
                       schematic=schematic.resolve())


@dataclass(frozen=True)
class ResolvedSource:
    source: Source
    base: Path
    repo_root: Path | None  # None when resolved from explicit options only


def resolve_source(cell_dir: Path, *, explicit_repository: str | None,
                    explicit_commit: str | None) -> ResolvedSource:
    """Resolve the source repository and commit from git or from explicit
    options; never guess them."""
    root = gitinfo.toplevel(cell_dir) or gitinfo.toplevel(cell_dir.parent)
    repository = explicit_repository
    commit = explicit_commit
    if root is not None:
        repository = repository or gitinfo.remote_url(root)
        commit = commit or gitinfo.head(root)
    if repository:
        repository = normalize_repository_url(repository)
    if not repository or not commit:
        raise UsageError(
            "cannot determine the source repository and commit (cell outside a git "
            "repository, or repository without an 'origin' remote); "
            "pass --source-repository and --source-commit"
        )
    base = root or cell_dir
    try:
        rel = cell_dir.resolve().relative_to(base.resolve())
        paths = (rel.as_posix() or ".",)
    except ValueError:
        paths = (cell_dir.name,)
    return ResolvedSource(Source(repository=repository, commit=commit, paths=paths), base, root)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def relative_to_base(path: Path, base: Path) -> str:
    try:
        rel = path.resolve().relative_to(base.resolve())
    except ValueError as exc:
        raise UsageError(
            f"{path} is outside {base}: --out must be under the source repository "
            "root (or under the cell directory outside git) so that manifest paths "
            "are relative"
        ) from exc
    return rel.as_posix()


def dirty_file_warning(repo_root: Path | None, commit: str, abs_path: Path) -> str | None:
    """Warning when ``abs_path`` differs from its content at ``commit``.

    ``None`` when identical or not comparable (outside git, file added
    later). Never blocking, so a designer can check uncommitted work, but the
    difference is always reported.
    """
    if repo_root is None:
        return None
    try:
        rel = abs_path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return None
    committed = gitinfo.blob_at_commit(repo_root, rel, commit)
    if committed is None or committed == abs_path.read_bytes():
        return None
    return (
        f"{rel} differs from its content at commit {commit[:12]}; the manifest cites "
        "that commit, but the file checked is the working copy (its SHA-256 in "
        "`inputs` identifies what was checked)"
    )


def pdk_dirty_warning(pdk_root: Path) -> str | None:
    if gitinfo.is_repo_dirty(pdk_root):
        return (
            f"the PDK under {pdk_root} has uncommitted changes; the recorded "
            "commit_sha may not describe the tree actually used"
        )
    return None


@dataclass(frozen=True)
class CheckOutcome:
    exit_code: int
    status: str
    message: str
    manifest: dict[str, Any] | None = None
    manifest_path: Path | None = None
    warnings: tuple[str, ...] = ()

    def to_json_dict(self) -> dict[str, Any]:
        """Stable JSON contract: the same keys on every exit path."""
        return {
            "exit_code": self.exit_code,
            "status": self.status,
            "message": self.message,
            "manifest_path": str(self.manifest_path) if self.manifest_path else None,
            "manifest": self.manifest,
            "warnings": list(self.warnings),
        }


_STATUS_USAGE_ERROR = "usage_error"
_STATUS_ENVIRONMENT_INCOMPLETE = "environment_incomplete"
_STATUS_TOOL_FAILURE = "tool_failure"
_STATUS_NOT_CLEAN = "not_clean"
_STATUS_OK = "ok"
_STATUS_DRY_RUN = "dry_run"
_STATUS_TIMEOUT = "timeout"
_STATUS_TOOL_ERROR = "tool_error"


def run_check(
    *,
    target: Path,
    out_dir: Path | None,
    pdk_root: str | None,
    pdk_variant: str | None,
    pdk_commit: str | None,
    cell_override: str | None,
    schematic_override: Path | None,
    source_repository: str | None,
    source_commit: str | None,
    dry_run: bool,
    disable_aslr_guard: bool,
    use_cache: bool = True,
    resume: bool = False,
    force: bool = False,
    with_klayout: bool = False,
    klayout_tech: str | None = None,
) -> CheckOutcome:
    """Public entry point: always returns a ``CheckOutcome`` with an exit
    code and a status, never a traceback.

    Usage errors, invalid provenance, tool time-outs and system errors
    (permissions, full disk) each map to their own status.
    ``KeyboardInterrupt`` is handled in ``cli.main``.
    """
    try:
        return _run_check(
            target=target, out_dir=out_dir, pdk_root=pdk_root, pdk_variant=pdk_variant,
            pdk_commit=pdk_commit, cell_override=cell_override,
            schematic_override=schematic_override, source_repository=source_repository,
            source_commit=source_commit, dry_run=dry_run, disable_aslr_guard=disable_aslr_guard,
            use_cache=use_cache, with_klayout=with_klayout, klayout_tech=klayout_tech,
            resume=resume, force=force,
        )
    except UsageError as exc:
        return CheckOutcome(exitcodes.USAGE_ERROR, _STATUS_USAGE_ERROR, str(exc))
    except ManifestValidationError as exc:
        return CheckOutcome(exitcodes.USAGE_ERROR, _STATUS_USAGE_ERROR,
                             f"invalid provenance: {exc}; check source and PDK commit values")
    except subprocess.TimeoutExpired as exc:
        return CheckOutcome(exitcodes.TOOL_FAILURE, _STATUS_TIMEOUT,
                             f"time-out while running {' '.join(exc.cmd)}: {exc}; check tool logs and system load")
    except OSError as exc:
        return CheckOutcome(exitcodes.TOOL_FAILURE, _STATUS_TOOL_ERROR,
                             f"system error: {exc}; check file paths, permissions, and free space")


_DEFAULT_PDK_VARIANT = "sky130A"


def _load_verify_config(cell_dir: Path) -> VerifyConfig:
    """``verify.toml`` from ``cell_dir`` if present, else an empty config.
    An invalid file raises :class:`UsageError`."""
    config_path = find_verify_config(cell_dir)
    if config_path is None:
        return VerifyConfig()
    try:
        return load_verify_config(config_path, base_dir=cell_dir)
    except VerifyConfigError as exc:
        raise UsageError(str(exc)) from exc


def _run_check(
    *,
    target: Path,
    out_dir: Path | None,
    pdk_root: str | None,
    pdk_variant: str | None,
    pdk_commit: str | None,
    cell_override: str | None,
    schematic_override: Path | None,
    source_repository: str | None,
    source_commit: str | None,
    dry_run: bool,
    disable_aslr_guard: bool,
    use_cache: bool,
    resume: bool,
    force: bool,
    with_klayout: bool = False,
    klayout_tech: str | None = None,
) -> CheckOutcome:
    # verify.toml fills what the command line leaves unset; explicit options
    # win. It is loaded before validation so that its values are validated
    # too.
    config_dir = target if target.is_dir() else target.parent
    config = _load_verify_config(config_dir)
    pdk_variant = pdk_variant or config.pdk_variant or _DEFAULT_PDK_VARIANT
    pdk_commit = pdk_commit or config.pdk_commit
    cell_override = cell_override or config.cell
    schematic_override = schematic_override or config.schematic
    source_repository = source_repository or config.source_repository
    source_commit = source_commit or config.source_commit

    # Validate every argument before running any tool.
    validate_git_sha(pdk_commit, "--pdk-commit")
    validate_git_sha(source_commit, "--source-commit")
    if source_repository:
        source_repository = normalize_repository_url(source_repository)
    validate_repository_url(source_repository, "--source-repository")
    # out_dir is made absolute before work_dir and cache_dir are derived
    # from it: Netgen runs with cwd=work_dir and would not resolve a
    # relative extracted-netlist path.
    inputs = resolve_cell_inputs(target, cell_override=cell_override,
                                  schematic_override=schematic_override,
                                  layout_override=config.layout)
    cell_dir = target if target.is_dir() else target.parent
    resolved = resolve_source(cell_dir, explicit_repository=source_repository,
                               explicit_commit=source_commit)
    source, base = resolved.source, resolved.base
    out_dir = (out_dir or base / "sky130-verify-out" / inputs.cell).resolve()
    # Check that the output stays under the base before running any tool.
    relative_to_base(out_dir, base)
    _guard_output_directory(out_dir, source=source, cell_id=inputs.cell,
                            resume=resume, force=force)

    warnings: list[str] = []
    for path in (inputs.layout, inputs.schematic):
        w = dirty_file_warning(resolved.repo_root, source.commit, path)
        if w:
            warnings.append(w)

    report = doctor.run_doctor(pdk_root, pdk_variant, pdk_commit)

    if dry_run and report.pdk.found:
        # --dry-run needs only the PDK paths (magicrc, Netgen setup), not the
        # Magic and Netgen binaries, so the plan can be shown before the tools
        # are installed. Without a resolved PDK, fall through to the
        # incomplete-environment result below.
        dry_warnings = list(warnings)
        missing = [t.name for t in report.tools if t.required and not t.found]
        if missing:
            dry_warnings.append(
                "not installed on this machine: " + ", ".join(missing)
                + "; the plan shows the commands that would run once installed "
                "(see `sky130-verify doctor`)"
            )
        return CheckOutcome(
            exitcodes.OK, _STATUS_DRY_RUN,
            _dry_run_plan(inputs, out_dir, Path(report.pdk.magicrc), Path(report.pdk.netgen_setup),
                          disable_aslr_guard),
            warnings=tuple(dry_warnings),
        )

    if not report.ok:
        return CheckOutcome(
            exitcodes.ENVIRONMENT_INCOMPLETE, _STATUS_ENVIRONMENT_INCOMPLETE,
            "incomplete environment; install the missing requirements shown below:\n"
            + doctor.format_report_human(report),
            warnings=tuple(warnings),
        )
    magicrc = Path(report.pdk.magicrc)
    netgen_setup = Path(report.pdk.netgen_setup)
    pdk_root_path = Path(report.pdk.root)

    if w := pdk_dirty_warning(pdk_root_path):
        warnings.append(w)

    # KLayout is checked separately: it is optional and DoctorReport.ok does
    # not depend on it.
    klayout_deck: Path | None = None
    if with_klayout:
        klayout_tool = doctor.probe_tool("klayout")
        klayout_tech_status = doctor.resolve_klayout_tech(klayout_tech)
        if not klayout_tool.found or not klayout_tech_status.found:
            detail = []
            if not klayout_tool.found:
                detail.append("klayout not found on PATH")
            if not klayout_tech_status.found:
                detail.append(klayout_tech_status.note)
            return CheckOutcome(
                exitcodes.ENVIRONMENT_INCOMPLETE, _STATUS_ENVIRONMENT_INCOMPLETE,
                "--with-klayout cannot run: " + "; ".join(detail)
                + "; install KLayout and set --klayout-tech, or rerun without --with-klayout",
                warnings=tuple(warnings),
            )
        klayout_deck = Path(klayout_tech_status.deck_path)

    # From here on report.ok holds and this is a real run (dry runs returned
    # above).

    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir = out_dir / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = out_dir / ".cache"

    magic_version = doctor.probe_tool("magic").version or "unknown"
    netgen_version = doctor.probe_tool("netgen").version or "unknown"
    # A version banner does not identify a binary: two local builds can print
    # the same version. The manifest records each executable's SHA-256 (not
    # its local path), and the cache keys include it.
    tool_paths = {status.name: status.path for status in report.tools if status.required}
    magic_path = tool_paths.get("magic")
    netgen_path = tool_paths.get("netgen")
    if not magic_path or not netgen_path:
        # Unreachable after report.ok; guards against a partial manifest.
        return CheckOutcome(exitcodes.ENVIRONMENT_INCOMPLETE, _STATUS_ENVIRONMENT_INCOMPLETE,
                            "incomplete environment: Magic or Netgen path not resolved",
                            warnings=tuple(warnings))
    magic_binary_hash = sha256_file(Path(magic_path))
    netgen_binary_hash = sha256_file(Path(netgen_path))
    layout_hash = sha256_file(inputs.layout)
    schematic_hash = sha256_file(inputs.schematic)
    # The verdict also depends on the PDK deck (magicrc, Netgen setup) and on
    # the Tcl scripts shipped in this package: record their digests and put
    # them in the cache keys.
    magicrc_hash = sha256_file(magicrc)
    netgen_setup_hash = sha256_file(netgen_setup)
    drc_tcl_hash = sha256_file(toolchain.TCL_DIR / "drc.tcl")
    extract_tcl_hash = sha256_file(toolchain.TCL_DIR / "extract.tcl")

    # Magic loads .mag cells by name, not by path; see
    # toolchain.run_extraction.
    if inputs.cell_format == "mag":
        view_for_magic = work_dir / f"{inputs.cell}.mag"
        shutil.copyfile(inputs.layout, view_for_magic)
    else:
        view_for_magic = inputs.layout

    run_log: dict[str, Any] = {"state": "running", "steps": []}
    # Written before the first subprocess: after a Ctrl-C or a crash, the
    # next invocation requires --resume or --force instead of silently
    # overwriting a state of unknown completeness.
    _write_json_atomic(out_dir / "run.json", run_log)

    drc_key = sha256_text("drc", layout_hash, magic_version, magic_binary_hash, report.pdk.commit_sha or "", pdk_variant,
                           magicrc_hash, drc_tcl_hash)
    drc_result, drc_cached, drc_seconds = _cached_step(
        cache_dir, "drc", drc_key, use_cache,
        lambda: toolchain.run_drc(
            view=view_for_magic, cell_format=inputs.cell_format, cell=inputs.cell,
            magicrc=magicrc, pdk_root=pdk_root_path, pdk_variant=pdk_variant, work_dir=work_dir,
            disable_aslr_guard=disable_aslr_guard,
        ),
        lambda r: {"ok": r.ok, "error_count": r.error_count, "verdict": r.verdict,
                   "log_path": str(r.command.log_path), "argv": list(r.command.argv)},
        lambda d: _FakeResult(ok=d["ok"], error_count=d["error_count"], verdict=d["verdict"],
                               log_path=Path(d["log_path"]), argv=tuple(d["argv"])),
    )
    run_log["steps"].append({"name": "drc", "cached": drc_cached, "wall_seconds": drc_seconds,
                              "verdict": drc_result.verdict, "argv": list(drc_result.argv)})

    extract_key = sha256_text("extract", layout_hash, magic_version, magic_binary_hash, report.pdk.commit_sha or "", pdk_variant,
                               magicrc_hash, extract_tcl_hash)
    extract_result, extract_cached, extract_seconds = _cached_step(
        cache_dir, "extract", extract_key, use_cache,
        lambda: toolchain.run_extraction(
            view=view_for_magic, cell_format=inputs.cell_format, cell=inputs.cell,
            magicrc=magicrc, pdk_root=pdk_root_path, pdk_variant=pdk_variant, work_dir=work_dir,
            disable_aslr_guard=disable_aslr_guard,
        ),
        lambda r: {"ok": r.ok, "spice_path": str(r.spice_path) if r.spice_path else None,
                   "log_path": str(r.command.log_path), "argv": list(r.command.argv)},
        lambda d: _FakeResult(ok=d["ok"], spice_path=Path(d["spice_path"]) if d["spice_path"] else None,
                               log_path=Path(d["log_path"]), argv=tuple(d["argv"])),
    )
    run_log["steps"].append({"name": "extract", "cached": extract_cached, "wall_seconds": extract_seconds,
                              "ok": extract_result.ok, "argv": list(extract_result.argv)})

    if extract_result.ok and extract_result.spice_path and extract_result.spice_path.is_file():
        lvs_key = sha256_text("lvs", extract_result.spice_path.name, sha256_file(extract_result.spice_path),
                               schematic_hash, netgen_version, netgen_binary_hash, report.pdk.commit_sha or "", pdk_variant,
                               netgen_setup_hash, toolchain.LVS_RULE_VERSION)
        lvs_result, lvs_cached, lvs_seconds = _cached_step(
            cache_dir, "lvs", lvs_key, use_cache,
            lambda: toolchain.run_lvs(
                extracted_spice=extract_result.spice_path, golden_spice=inputs.schematic,
                cell=inputs.cell, netgen_setup=netgen_setup, pdk_root=pdk_root_path,
                pdk_variant=pdk_variant, work_dir=work_dir,
            ),
            lambda r: {"verdict": r.verdict, "log_path": str(r.command.log_path), "argv": list(r.command.argv),
                       "comparison_path": str(r.comparison_path) if r.comparison_path else None,
                       "black_boxes": list(r.black_boxes)},
            lambda d: _FakeResult(verdict=d["verdict"], log_path=Path(d["log_path"]), argv=tuple(d["argv"]),
                                   comparison_path=Path(d["comparison_path"]) if d.get("comparison_path") else None,
                                   black_boxes=tuple(d["black_boxes"])),
        )
        lvs_verdict = lvs_result.verdict
        lvs_log = lvs_result.log_path
        lvs_comparison = lvs_result.comparison_path
        if lvs_result.black_boxes:
            warnings.append(
                "LVS inconclusive: undefined subcircuit(s) that are not PDK setup primitives "
                "were compared as black boxes, without checking their content: "
                + ", ".join(lvs_result.black_boxes))
        run_log["steps"].append({"name": "lvs", "cached": lvs_cached, "wall_seconds": lvs_seconds,
                                  "verdict": lvs_verdict, "argv": list(lvs_result.argv),
                                  "black_boxes": list(lvs_result.black_boxes)})
    else:
        lvs_verdict = "error"
        lvs_log = extract_result.log_path
        lvs_comparison = None

    drc_verdict = drc_result.verdict
    drc_log = drc_result.log_path

    tools = {"magic": magic_version, "netgen": netgen_version}
    toolchain_block = {
        "tools": tools,
        "executables": {
            "magic": {"sha256": magic_binary_hash},
            "netgen": {"sha256": netgen_binary_hash},
        },
        "pdk_commit_source": report.pdk.commit_source,
        "pdk_files": {"magicrc_sha256": magicrc_hash, "netgen_setup_sha256": netgen_setup_hash},
        "scripts": {"drc_tcl_sha256": drc_tcl_hash, "extract_tcl_sha256": extract_tcl_hash},
    }

    # KLayout DRC cross-check: recorded separately, never merged with the
    # Magic DRC verdict, no effect on the exit code. There is no KLayout LVS
    # cross-check.
    klayout_gds: Path | None = None
    klayout_drc_result: toolchain.KlayoutDrcResult | None = None
    if with_klayout and klayout_deck is not None:
        if inputs.cell_format == "gds":
            klayout_gds = inputs.layout
        else:
            gdswrite_tcl_hash = sha256_file(toolchain.TCL_DIR / "gdswrite.tcl")
            toolchain_block["scripts"]["gdswrite_tcl_sha256"] = gdswrite_tcl_hash
            gdswrite_key = sha256_text("gdswrite", layout_hash, magic_version, magic_binary_hash,
                                        report.pdk.commit_sha or "", pdk_variant, magicrc_hash,
                                        gdswrite_tcl_hash)
            gdswrite_result, gdswrite_cached, gdswrite_seconds = _cached_step(
                cache_dir, "gdswrite", gdswrite_key, use_cache,
                lambda: toolchain.run_gds_write(
                    view=view_for_magic, cell=inputs.cell, magicrc=magicrc,
                    pdk_root=pdk_root_path, pdk_variant=pdk_variant, work_dir=work_dir,
                    disable_aslr_guard=disable_aslr_guard,
                ),
                lambda r: {"ok": r.ok, "gds_path": str(r.gds_path) if r.gds_path else None,
                           "log_path": str(r.command.log_path), "argv": list(r.command.argv)},
                lambda d: _FakeResult(ok=d["ok"],
                                       spice_path=Path(d["gds_path"]) if d["gds_path"] else None,
                                       log_path=Path(d["log_path"]), argv=tuple(d["argv"])),
            )
            run_log["steps"].append({"name": "gdswrite", "cached": gdswrite_cached,
                                      "wall_seconds": gdswrite_seconds, "ok": gdswrite_result.ok,
                                      "argv": list(gdswrite_result.argv)})
            if gdswrite_result.ok and gdswrite_result.spice_path:
                klayout_gds = gdswrite_result.spice_path

        if klayout_gds is not None:
            klayout_drc_result = toolchain.run_klayout_drc(
                gds=klayout_gds, cell=inputs.cell, deck=klayout_deck, work_dir=work_dir,
            )
            klayout_version = doctor.probe_tool("klayout").version or "unknown"
            # Record the deck as a path relative to the technology root plus
            # its SHA-256: portable, and no local directory layout leaks into
            # the manifest.
            klayout_deck_hash = sha256_file(klayout_deck)
            klayout_root = Path(klayout_tech_status.root)
            klayout_deck_rel = klayout_deck.resolve().relative_to(klayout_root.resolve()).as_posix()
            klayout_path = klayout_tool.path
            klayout_binary_hash = sha256_file(Path(klayout_path)) if klayout_path else None
            toolchain_block["klayout_cross_check"] = {
                "drc_verdict": klayout_drc_result.verdict,
                "violation_count": klayout_drc_result.violation_count,
                "version": klayout_version,
                "deck": klayout_deck_rel,
                "deck_sha256": klayout_deck_hash,
                "executable_sha256": klayout_binary_hash,
            }
            run_log["steps"].append({
                "name": "klayout_drc", "cached": False,
                "verdict": klayout_drc_result.verdict, "argv": list(klayout_drc_result.command.argv),
            })
        else:
            toolchain_block["klayout_cross_check"] = {
                "drc_verdict": "error",
                "violation_count": None,
                "version": doctor.probe_tool("klayout").version or "unknown",
                "deck": "drc/sky130A_mr.drc",
                "deck_sha256": sha256_file(klayout_deck),
                "executable_sha256": sha256_file(Path(klayout_tool.path)) if klayout_tool.path else None,
            }

    input_hashes = {
        relative_to_base(inputs.layout, base): layout_hash,
        relative_to_base(inputs.schematic, base): schematic_hash,
    }

    report_md = _render_report(inputs, drc_verdict, drc_result.error_count, lvs_verdict, tools, warnings,
                                klayout_cross_check=toolchain_block.get("klayout_cross_check"),
                                layout_label=relative_to_base(inputs.layout, base),
                                schematic_label=relative_to_base(inputs.schematic, base))
    (out_dir / "report.md").write_text(report_md, encoding="utf-8")

    run_log["state"] = "completed"
    run_log["warnings"] = warnings
    run_log["cache_used"] = use_cache
    _write_json_atomic(out_dir / "run.json", run_log)

    artifact_paths = {
        "report.md": out_dir / "report.md",
        "run.json": out_dir / "run.json",
        "logs/drc.log": drc_log,
        "logs/extract.log": extract_result.log_path,
        "logs/lvs.log": lvs_log,
    }
    if lvs_comparison and lvs_comparison.is_file():
        # Netgen's detailed device/pin matching.
        artifact_paths["logs/lvs.out"] = lvs_comparison
    if klayout_drc_result is not None:
        artifact_paths["logs/klayout-drc.log"] = klayout_drc_result.command.log_path
        if klayout_drc_result.report_path and klayout_drc_result.report_path.is_file():
            artifact_paths["logs/klayout-drc-report.xml"] = klayout_drc_result.report_path
    artifacts = {}
    for label, path in artifact_paths.items():
        dest = out_dir / label
        if path != dest:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
        artifacts[relative_to_base(dest, base)] = sha256_file(dest)

    try:
        manifest = VerificationManifest(
            schema_version="1.0.0",
            cell_id=inputs.cell,
            source=source,
            pdk=Pdk(family="sky130", variant=pdk_variant, commit_sha=report.pdk.commit_sha,
                    repository="https://github.com/RTimothyEdwards/open_pdks"),
            toolchain=toolchain_block,
            inputs=input_hashes,
            verification=Verification(
                drc_verdict=drc_verdict, lvs_verdict=lvs_verdict,
                drc_log=relative_to_base(out_dir / "logs/drc.log", base),
                lvs_log=relative_to_base(out_dir / "logs/lvs.log", base),
            ),
            artifacts=artifacts,
        )
    except ManifestValidationError as exc:
        return CheckOutcome(exitcodes.TOOL_FAILURE, _STATUS_TOOL_FAILURE,
                             f"manifest invalid after a complete run: {exc}",
                             warnings=tuple(warnings))

    manifest_dict = manifest.to_dict()
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(manifest.to_json(), encoding="utf-8")

    # Badges derive from the manifest and are not hashed in it (that would
    # be circular); rendered afterwards, they embed the manifest's digest.
    manifest_sha256 = sha256_file(manifest_path)
    badge_files = badge.render_all(inputs.cell, drc_verdict, lvs_verdict,
                                   manifest_sha256=manifest_sha256)
    for name, content in badge_files.items():
        (out_dir / name).write_text(content, encoding="utf-8")

    verdicts = {drc_verdict, lvs_verdict}
    if verdicts & {"error", "unknown"}:
        # "unknown" is not a failed check but an inconclusive result: exit
        # code 4, not 1.
        code, status = exitcodes.TOOL_FAILURE, _STATUS_TOOL_FAILURE
    elif verdicts == {"pass"}:
        code, status = exitcodes.OK, _STATUS_OK
    else:
        code, status = exitcodes.NOT_CLEAN, _STATUS_NOT_CLEAN

    next_step = (f"; inspect {out_dir / 'logs'}" if code == exitcodes.TOOL_FAILURE else
                 f"; inspect {out_dir / 'report.md'}" if code == exitcodes.NOT_CLEAN else "")
    return CheckOutcome(code, status, f"drc={drc_verdict} lvs={lvs_verdict}; manifest: {manifest_path}{next_step}",
                         manifest=manifest_dict, manifest_path=manifest_path, warnings=tuple(warnings))


@dataclass(frozen=True)
class _FakeResult:
    """Toolchain result rebuilt from the cache, with only the fields
    ``_run_check`` reads."""
    ok: bool = True
    error_count: int | None = None
    verdict: str = "unknown"
    spice_path: Path | None = None
    comparison_path: Path | None = None
    log_path: Path = field(default_factory=lambda: Path("."))
    argv: tuple[str, ...] = ()
    black_boxes: tuple[str, ...] = ()


def _cached_step(cache_dir: Path, kind: str, key: str, use_cache: bool, run, to_dict, from_dict):
    cache_path = cache_dir / f"{kind}-{key}.json"
    if use_cache and cache_path.is_file():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            result = from_dict(data)
            log_path = getattr(result, "log_path", None)
            spice_path = getattr(result, "spice_path", None)
            comparison_path = getattr(result, "comparison_path", None)
            # Reuse a cached result only if the files it references still
            # exist; otherwise recompute.
            if (
                (log_path is None or Path(log_path).is_file())
                and (spice_path is None or Path(spice_path).is_file())
                and (comparison_path is None or Path(comparison_path).is_file())
            ):
                return result, True, 0.0
        except (OSError, json.JSONDecodeError, KeyError):
            pass
    started = time.monotonic()
    result = run()
    elapsed = time.monotonic() - started
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = to_dict(result)
    payload["argv"] = list(result.command.argv)
    _write_json_atomic(cache_path, payload)
    normalized = from_dict(payload)
    return normalized, False, elapsed


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write via a temporary file so an interruption never leaves truncated JSON."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _guard_output_directory(out_dir: Path, *, source: Source, cell_id: str,
                            resume: bool, force: bool) -> None:
    """Refuse to overwrite an output produced for another cell or provenance."""
    if force:
        return
    manifest_path = out_dir / "manifest.json"
    if manifest_path.is_file():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UsageError(
                f"unreadable existing output in {out_dir} ({exc}); choose another --out or pass --force"
            ) from exc
        previous_source = previous.get("source", {})
        if (previous.get("cell_id") != cell_id
                or previous_source.get("repository") != source.repository
                or previous_source.get("commit") != source.commit):
            raise UsageError(
                f"existing output in {out_dir} belongs to another cell or provenance; "
                "choose another --out or pass --force"
            )
    run_path = out_dir / "run.json"
    if run_path.is_file():
        try:
            state = json.loads(run_path.read_text(encoding="utf-8")).get("state")
        except (OSError, json.JSONDecodeError) as exc:
            raise UsageError(
                f"unreadable run state in {out_dir} ({exc}); choose another --out or pass --force"
            ) from exc
        if state == "running" and not resume:
            raise UsageError(
                f"a run in {out_dir} is marked as running or interrupted; "
                "make sure no process is using it, then rerun with --resume, or choose another --out"
            )


def _dry_run_plan(inputs: CellInputs, out_dir: Path, magicrc: Path, netgen_setup: Path,
                   disable_aslr_guard: bool) -> str:
    magic_argv = toolchain.aslr_guard_command(
        ["magic", "-dnull", "-noconsole", "-rcfile", str(magicrc), "<tcl/{drc,extract}.tcl>"],
        disabled=disable_aslr_guard,
    )
    netgen_argv = ["netgen", "-batch", "lvs", "<extracted> <cell>", "<reference> <cell>",
                   str(netgen_setup), "<output.out>"]
    plan = [
        f"mkdir -p {out_dir}",
        (f"cp {inputs.layout} {out_dir}/work/{inputs.cell}.{inputs.cell_format}"
         if inputs.cell_format == "mag" else
         "# .gds: read from its path, no copy needed"),
        " ".join(magic_argv) + "   # DRC, then extraction (two invocations: tcl/drc.tcl, tcl/extract.tcl)",
        " ".join(netgen_argv),
    ]
    return "\n".join(plan)


def _render_report(inputs: CellInputs, drc_verdict: str, drc_error_count: int | None,
                    lvs_verdict: str, tools: dict[str, str], warnings: list[str],
                    klayout_cross_check: dict[str, Any] | None = None,
                    layout_label: str | None = None, schematic_label: str | None = None) -> str:
    lines = [
        f"# sky130-verify: {inputs.cell}",
        "",
        "| Check | Verdict |",
        "|---|---|",
        f"| DRC | {drc_verdict}" + (f" ({drc_error_count} errors)" if drc_error_count is not None else "") + " |",
        f"| LVS | {lvs_verdict} |",
        "",
        f"Layout: `{layout_label or inputs.layout.name}` (`{inputs.cell_format}`)",
        "",
        f"Reference schematic: `{schematic_label or inputs.schematic.name}`",
        "",
        f"Tools: magic {tools.get('magic')}; netgen {tools.get('netgen')}",
    ]
    if klayout_cross_check is not None:
        # Separate section, never merged with the verdicts above, so that a
        # disagreement between KLayout and Magic stays visible.
        count = klayout_cross_check.get("violation_count")
        lines += [
            "",
            "## KLayout DRC (informational)",
            "",
            f"Verdict: {klayout_cross_check.get('drc_verdict')}"
            + (f" ({count} violations)" if count is not None else ""),
            f"Tool: {klayout_cross_check.get('version')}",
            f"Deck: `{klayout_cross_check.get('deck')}`",
        ]
    if warnings:
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in warnings]
    return "\n".join(lines) + "\n"
