"""``sky130-verify`` command-line entry point.

Every command supports structured output through ``--json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from . import __version__, badge, doctor, exitcodes
from .batch import run_batch
from .check import UsageError, run_check, validate_git_sha
from .manifest_cmd import render_manifest_markdown, validate_manifest_file, verify_manifest_files

_EXIT_CODE_HELP = (
    "Exit codes: 0 clean · 1 non-conformant · 2 usage error · "
    "3 incomplete environment · 4 tool failure/ambiguity · 5 refused (out of scope)."
)


def _json_payload(exit_code: int, status: str, message: str, **details: object) -> dict[str, object]:
    """Envelope shared by every command: exit_code/status/message plus
    command-specific details."""
    return {"exit_code": exit_code, "status": status, "message": message, **details}


def _status_for_exit(exit_code: int) -> str:
    return {
        exitcodes.OK: "ok",
        exitcodes.NOT_CLEAN: "not_clean",
        exitcodes.USAGE_ERROR: "usage_error",
        exitcodes.ENVIRONMENT_INCOMPLETE: "environment_incomplete",
        exitcodes.TOOL_FAILURE: "tool_failure",
        exitcodes.OUT_OF_SCOPE: "refused",
    }.get(exit_code, "error")


class _CliArgumentParser(argparse.ArgumentParser):
    """Raises UsageError instead of exiting, so --json is honored even
    on a parse error."""

    def error(self, message: str) -> None:
        raise UsageError(message)


def _build_parser() -> argparse.ArgumentParser:
    parser = _CliArgumentParser(
        prog="sky130-verify",
        description="Headless DRC/LVS verification of a single Sky130A cell.",
        epilog=(
            "Example:\n"
            "  sky130-verify doctor\n"
            "  sky130-verify check cells/my_opamp --pdk-root \"$PDK_ROOT\" "
            "--pdk-commit \"$PDK_COMMIT\" --out out/\n\n"
            f"{_EXIT_CODE_HELP}\n"
            "Detailed options: sky130-verify <command> --help"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"sky130-verify {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_CliArgumentParser)

    p_doctor = sub.add_parser(
        "doctor", help="environment diagnostics (Magic/Netgen/KLayout/PDK)",
        epilog=f"Example: sky130-verify doctor --pdk-root \"$PDK_ROOT\"\n\n{_EXIT_CODE_HELP}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_doctor.add_argument("--pdk-root", default=None, help="PDK root (default: $PDK_ROOT)")
    p_doctor.add_argument("--pdk-variant", default="sky130A")
    p_doctor.add_argument("--pdk-commit", default=None,
                           help="open_pdks commit SHA (otherwise detected if --pdk-root is a git clone)")
    p_doctor.add_argument("--klayout-tech", default=None,
                           help="KLayout technology root for --with-klayout "
                                "(default: $KLAYOUT_TECH_PATH) — optional")
    p_doctor.add_argument("--json", action="store_true", help="structured JSON output on stdout")

    p_check = sub.add_parser(
        "check", help="verify a cell (DRC then LVS)",
        epilog=(
            "Examples:\n"
            "  sky130-verify check cells/my_opamp --pdk-root \"$PDK_ROOT\"\n"
            "  sky130-verify check cells/my_opamp --pdk-root \"$PDK_ROOT\" --dry-run\n"
            "  sky130-verify check cells/my_opamp --pdk-root \"$PDK_ROOT\" --json | jq .status\n\n"
            f"{_EXIT_CODE_HELP}"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_check.add_argument("target", type=Path, help="cell directory or layout file")
    p_check.add_argument("--out", type=Path, default=None,
                          help="output directory (default: ./sky130-verify-out/<cell>)")
    p_check.add_argument("--pdk-root", default=None, help="PDK root (default: $PDK_ROOT)")
    p_check.add_argument("--pdk-variant", default=None,
                          help="default: verify.toml [pdk].variant, otherwise sky130A")
    p_check.add_argument("--pdk-commit", default=None,
                          help="open_pdks commit SHA (otherwise detected if --pdk-root is a git clone)")
    p_check.add_argument("--cell", default=None,
                          help="cell name (default: verify.toml [cell].name, otherwise the layout file's name)")
    p_check.add_argument("--schematic", type=Path, default=None,
                          help="explicit golden schematic (default: verify.toml [cell].schematic, "
                               "otherwise the directory's single file)")
    p_check.add_argument("--source-repository", default=None,
                          help="source repository URL (otherwise detected via git)")
    p_check.add_argument("--source-commit", default=None,
                          help="source commit SHA (otherwise detected via git)")
    p_check.add_argument("--dry-run", "-n", action="store_true",
                          help="show the commands that would run, without running anything")
    p_check.add_argument("--no-aslr-guard", action="store_true",
                          help="don't prefix Magic calls with setarch -R")
    p_check.add_argument("--no-cache", action="store_true",
                          help="ignore the DRC/extraction/LVS cache and re-run everything")
    p_check.add_argument("--resume", action="store_true",
                          help="explicitly resume an output marked interrupted/incomplete")
    p_check.add_argument("--force", action="store_true",
                          help="allow overwriting output belonging to a different provenance")
    p_check.add_argument("--with-klayout", action="store_true",
                          help="add a KLayout DRC cross-check, recorded separately from "
                               "Magic's verdict and exit code")
    p_check.add_argument("--klayout-tech", default=None,
                          help="KLayout technology root (default: $KLAYOUT_TECH_PATH), "
                               "required with --with-klayout")
    p_check.add_argument("--json", action="store_true", help="structured JSON output on stdout")

    p_batch = sub.add_parser(
        "batch", help="verify several independent cells in one invocation",
        epilog=(
            "Example:\n"
            "  sky130-verify batch cells/inv cells/nand2 cells/nor2 --pdk-root \"$PDK_ROOT\" "
            "--out-root out/\n\n"
            "Each argument is a complete electrical cell and is verified independently. "
            "Overall exit code = the worst of the individual codes.\n\n"
            f"{_EXIT_CODE_HELP}"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_batch.add_argument("targets", type=Path, nargs="+", help="one or more cell directories")
    p_batch.add_argument("--out-root", type=Path, default=None,
                          help="output root directory, one subdirectory per cell "
                               "(default: ./sky130-verify-out)")
    p_batch.add_argument("--pdk-root", default=None, help="PDK root (default: $PDK_ROOT)")
    p_batch.add_argument("--pdk-variant", default=None,
                          help="default: verify.toml [pdk].variant per cell, otherwise sky130A")
    p_batch.add_argument("--pdk-commit", default=None,
                          help="open_pdks commit SHA (otherwise detected if --pdk-root is a git clone)")
    p_batch.add_argument("--dry-run", "-n", action="store_true",
                          help="show the commands that would run, without running anything")
    p_batch.add_argument("--no-aslr-guard", action="store_true",
                          help="don't prefix Magic calls with setarch -R")
    p_batch.add_argument("--no-cache", action="store_true",
                          help="ignore the DRC/extraction/LVS cache and re-run everything")
    p_batch.add_argument("--resume", action="store_true",
                          help="explicitly resume the batch's interrupted outputs")
    p_batch.add_argument("--force", action="store_true",
                          help="allow overwriting output from a different provenance")
    p_batch.add_argument("--fail-fast", action="store_true",
                          help="stop at the first nonzero exit code instead of "
                               "running every cell")
    p_batch.add_argument("--with-klayout", action="store_true",
                          help="add a per-cell KLayout DRC cross-check, recorded separately "
                               "from Magic's verdict and exit code")
    p_batch.add_argument("--klayout-tech", default=None,
                          help="KLayout technology root (default: $KLAYOUT_TECH_PATH), "
                               "required with --with-klayout")
    p_batch.add_argument("--json", action="store_true", help="structured JSON output on stdout")

    p_manifest = sub.add_parser("manifest", help="validate or show an existing manifest")
    manifest_sub = p_manifest.add_subparsers(dest="manifest_command", required=True,
                                              parser_class=_CliArgumentParser)
    p_mvalidate = manifest_sub.add_parser("validate", help="validate offline against the 1.0.0 schema")
    p_mvalidate.add_argument("manifest", type=Path)
    p_mvalidate.add_argument("--verify-files", type=Path, metavar="ROOT",
                             help="also check the SHA-256 of inputs/artifacts under ROOT, without re-running EDA")
    p_mvalidate.add_argument("--json", action="store_true", help="structured JSON output on stdout")
    p_mshow = manifest_sub.add_parser("show", help="readable Markdown rendering of a manifest")
    p_mshow.add_argument("manifest", type=Path)
    p_mshow.add_argument("--json", action="store_true", help="structured JSON output on stdout")

    p_badge = sub.add_parser("badge", help="generate a badge from a manifest")
    badge_sub = p_badge.add_subparsers(dest="badge_command", required=True)
    p_brender = badge_sub.add_parser("render", help="render badge.svg/badge.json/snippet.md")
    p_brender.add_argument("manifest", type=Path)
    p_brender.add_argument("--out", type=Path, default=None, help="default: the manifest's directory")
    p_brender.add_argument("--json", action="store_true", help="structured JSON output on stdout")

    return parser


def _cmd_doctor(args: argparse.Namespace) -> int:
    validate_git_sha(args.pdk_commit, "--pdk-commit")
    report = doctor.run_doctor(args.pdk_root, args.pdk_variant, args.pdk_commit, args.klayout_tech)
    code = exitcodes.OK if report.ok else exitcodes.ENVIRONMENT_INCOMPLETE
    if args.json:
        status = _status_for_exit(code)
        message = "environment ready for check" if report.ok else "incomplete environment"
        print(json.dumps(_json_payload(code, status, message, report=report.to_dict()),
                         ensure_ascii=False, indent=2))
    else:
        print(doctor.format_report_human(report))
    return code


def _cmd_check(args: argparse.Namespace) -> int:
    target: Path = args.target
    out_dir = args.out

    outcome = run_check(
        target=target,
        out_dir=out_dir,
        pdk_root=args.pdk_root,
        pdk_variant=args.pdk_variant,
        pdk_commit=args.pdk_commit,
        cell_override=args.cell,
        schematic_override=args.schematic,
        source_repository=args.source_repository,
        source_commit=args.source_commit,
        dry_run=args.dry_run,
        disable_aslr_guard=args.no_aslr_guard,
        use_cache=not args.no_cache,
        resume=args.resume,
        force=args.force,
        with_klayout=args.with_klayout,
        klayout_tech=args.klayout_tech,
    )
    if args.json:
        print(json.dumps(outcome.to_json_dict(), ensure_ascii=False, indent=2))
    else:
        print(outcome.message)
    for w in outcome.warnings:
        print(f"warning: {w}", file=sys.stderr)
    return outcome.exit_code


def _cmd_batch(args: argparse.Namespace) -> int:
    out_root = args.out_root
    outcome = run_batch(
        targets=tuple(args.targets),
        out_root=out_root,
        pdk_root=args.pdk_root,
        pdk_variant=args.pdk_variant,
        pdk_commit=args.pdk_commit,
        dry_run=args.dry_run,
        disable_aslr_guard=args.no_aslr_guard,
        use_cache=not args.no_cache,
        resume=args.resume,
        force=args.force,
        fail_fast=args.fail_fast,
        with_klayout=args.with_klayout,
        klayout_tech=args.klayout_tech,
    )
    if args.json:
        status = _status_for_exit(outcome.exit_code)
        print(json.dumps(_json_payload(outcome.exit_code, status,
                                       "batch clean" if outcome.exit_code == exitcodes.OK else "batch not clean",
                                       cells=outcome.to_json_dict()["cells"]),
                         ensure_ascii=False, indent=2))
    else:
        for label, cell_outcome in outcome.results:
            print(f"{label}: {cell_outcome.message}")
            for w in cell_outcome.warnings:
                print(f"warning ({label}): {w}", file=sys.stderr)
    return outcome.exit_code


def _cmd_manifest(args: argparse.Namespace) -> int:
    if args.manifest_command == "validate":
        ok, errors = validate_manifest_file(args.manifest)
        checked_files = 0
        if ok and args.verify_files is not None:
            ok, errors, checked_files = verify_manifest_files(args.manifest, args.verify_files)
        code = exitcodes.OK if ok else exitcodes.NOT_CLEAN
        if getattr(args, "json", False):
            print(json.dumps(_json_payload(code, "ok" if ok else "not_clean",
                                           "manifest valid" if ok else "manifest invalid",
                                           valid=ok, errors=errors, checked_files=checked_files),
                             ensure_ascii=False, indent=2))
        elif ok:
            print("valid")
        else:
            for err in errors:
                print(err, file=sys.stderr)
        return code
    if args.manifest_command == "show":
        if args.json:
            try:
                manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                print(json.dumps(_json_payload(exitcodes.USAGE_ERROR, "usage_error",
                                               f"invalid read/JSON: {exc}", manifest=None),
                                 ensure_ascii=False, indent=2))
                return exitcodes.USAGE_ERROR
            print(json.dumps(_json_payload(exitcodes.OK, "ok", "manifest read",
                                           manifest=manifest), ensure_ascii=False, indent=2))
        else:
            print(render_manifest_markdown(args.manifest))
        return exitcodes.OK
    raise AssertionError("unknown manifest subcommand")


def _cmd_badge(args: argparse.Namespace) -> int:
    if args.badge_command != "render":
        raise AssertionError("unknown badge subcommand")
    # Refuse to render from a manifest that isn't schema-valid.
    ok, errors = validate_manifest_file(args.manifest)
    if not ok:
        if args.json:
            print(json.dumps(_json_payload(exitcodes.NOT_CLEAN, "not_clean", "invalid manifest",
                                           errors=errors, output_dir=None), ensure_ascii=False, indent=2))
        else:
            for err in errors:
                print(err, file=sys.stderr)
        return exitcodes.NOT_CLEAN
    manifest_bytes = args.manifest.read_bytes()
    data = json.loads(manifest_bytes)
    verification = data["verification"]
    cell_id = data["cell_id"]
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    files = badge.render_all(cell_id, verification["drc_verdict"], verification["lvs_verdict"],
                              manifest_sha256=manifest_sha256)
    out_dir = args.out or args.manifest.resolve().parent
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (out_dir / name).write_text(content, encoding="utf-8")
    if args.json:
        print(json.dumps(_json_payload(exitcodes.OK, "ok", "badge written",
                                       output_dir=str(out_dir), files=sorted(files)),
                         ensure_ascii=False, indent=2))
    else:
        print(f"badge written under {out_dir}")
    return exitcodes.OK


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    effective_argv = sys.argv[1:] if argv is None else argv
    try:
        args = parser.parse_args(effective_argv)
        if args.command == "doctor":
            return _cmd_doctor(args)
        if args.command == "check":
            return _cmd_check(args)
        if args.command == "batch":
            return _cmd_batch(args)
        if args.command == "manifest":
            return _cmd_manifest(args)
        if args.command == "badge":
            return _cmd_badge(args)
    except UsageError as exc:
        if "--json" in effective_argv:
            print(json.dumps(_json_payload(exitcodes.USAGE_ERROR, "usage_error", str(exc)),
                             ensure_ascii=False, indent=2))
        else:
            print(f"usage error: {exc}", file=sys.stderr)
        return exitcodes.USAGE_ERROR
    except (OSError, json.JSONDecodeError) as exc:
        if "--json" in effective_argv:
            print(json.dumps(_json_payload(exitcodes.USAGE_ERROR, "usage_error",
                                           f"invalid read/JSON: {exc}"),
                             ensure_ascii=False, indent=2))
        else:
            print(f"usage error: invalid read/JSON: {exc}", file=sys.stderr)
        return exitcodes.USAGE_ERROR
    except KeyboardInterrupt:
        # 128 + SIGINT, the shell convention, instead of a raw traceback.
        if "--json" in effective_argv:
            print(json.dumps(_json_payload(130, "interrupted", "interrupted (SIGINT)"),
                             ensure_ascii=False, indent=2))
        else:
            print("interrupted (SIGINT)", file=sys.stderr)
        return 130
    parser.error("unknown subcommand")
    return exitcodes.USAGE_ERROR


if __name__ == "__main__":
    sys.exit(main())
