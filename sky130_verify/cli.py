"""Command-line entry point for Sky130A cell verification."""

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
    "Exit codes: 0 clean, 1 not clean, 2 usage error, 3 incomplete environment, "
    "4 tool failure or inconclusive result, 130 interrupted."
)

_PDK_ROOT_HELP = "PDK root directory (default: $PDK_ROOT)"
_PDK_COMMIT_HELP = "open_pdks commit SHA (default: HEAD of --pdk-root when it is a git clone)"
_JSON_HELP = "print a single JSON object on stdout"
_DRY_RUN_HELP = "show the planned tool invocations without running them"
_NO_ASLR_HELP = "do not prefix Magic invocations with setarch -R"
_NO_CACHE_HELP = "ignore the DRC/extraction/LVS cache and rerun every step"
_KLAYOUT_HELP = "run an informational KLayout DRC and record its separate result"
_KLAYOUT_TECH_HELP = ("technology directory containing drc/sky130A_mr.drc "
                      "(default: $KLAYOUT_TECH_PATH); required with --with-klayout")


def _json_payload(exit_code: int, status: str, message: str, **details: object) -> dict[str, object]:
    """Build the JSON response shared by all commands."""
    return {"exit_code": exit_code, "status": status, "message": message, **details}


def _status_for_exit(exit_code: int) -> str:
    return {
        exitcodes.OK: "ok",
        exitcodes.NOT_CLEAN: "not_clean",
        exitcodes.USAGE_ERROR: "usage_error",
        exitcodes.ENVIRONMENT_INCOMPLETE: "environment_incomplete",
        exitcodes.TOOL_FAILURE: "tool_failure",
    }.get(exit_code, "error")


def _manifest_input_error(path: Path) -> str | None:
    if not path.is_file():
        return f"manifest file {path} does not exist or is not a regular file; pass an existing manifest.json"
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return f"cannot read JSON manifest {path}: {exc}; pass a UTF-8 manifest.json"
    return None


class _CliArgumentParser(argparse.ArgumentParser):
    """Raise argument errors so ``--json`` can report them."""

    def error(self, message: str) -> None:
        raise UsageError(f"{message}; run '{self.prog} --help' for usage")


def _build_parser() -> argparse.ArgumentParser:
    parser = _CliArgumentParser(
        prog="sky130-verify",
        description="Verify Sky130A cell layouts with Magic DRC and Netgen LVS.",
        epilog=(
            "Examples:\n"
            "  sky130-verify doctor --pdk-root \"$PDK_ROOT\"\n"
            "  sky130-verify check \"$CELL_DIR\" --pdk-root \"$PDK_ROOT\" "
            "--pdk-commit \"$PDK_COMMIT\"\n\n"
            f"{_EXIT_CODE_HELP}\n"
            "Command options: sky130-verify <command> --help"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"sky130-verify {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_CliArgumentParser,
                                title="commands", metavar="<command>")

    p_doctor = sub.add_parser(
        "doctor", help="check tools and PDK; report what is missing",
        description="Check Magic, Netgen and the PDK; report missing requirements.",
        epilog=f"Example: sky130-verify doctor --pdk-root \"$PDK_ROOT\"\n\n{_EXIT_CODE_HELP}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_doctor.add_argument("--pdk-root", default=None, help=_PDK_ROOT_HELP)
    p_doctor.add_argument("--pdk-variant", default="sky130A", help="PDK variant (default: sky130A)")
    p_doctor.add_argument("--pdk-commit", default=None, help=_PDK_COMMIT_HELP)
    p_doctor.add_argument("--klayout-tech", default=None,
                           help="technology directory containing drc/sky130A_mr.drc "
                                "(default: $KLAYOUT_TECH_PATH)")
    p_doctor.add_argument("--json", action="store_true", help=_JSON_HELP)

    p_check = sub.add_parser(
        "check", help="run DRC and LVS for one cell; write a manifest and report",
        description="Run Magic DRC and Netgen LVS for one cell; write verdicts and evidence.",
        epilog=(
            "Examples:\n"
            "  sky130-verify check \"$CELL_DIR\" --pdk-root \"$PDK_ROOT\" "
            "--pdk-commit \"$PDK_COMMIT\"\n\n"
            f"{_EXIT_CODE_HELP}"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_check.add_argument("target", type=Path, help="cell directory or layout file")
    p_check.add_argument("--out", type=Path, default=None,
                          help="output directory (default: <repository root>/sky130-verify-out/<cell>, "
                               "or <cell directory>/sky130-verify-out/<cell> outside git)")
    p_check.add_argument("--pdk-root", default=None, help=_PDK_ROOT_HELP)
    p_check.add_argument("--pdk-variant", default=None,
                          help="PDK variant (default: verify.toml [pdk].variant, else sky130A)")
    p_check.add_argument("--pdk-commit", default=None, help=_PDK_COMMIT_HELP)
    p_check.add_argument("--cell", default=None,
                          help="cell name (default: verify.toml [cell].name, else the layout file name)")
    p_check.add_argument("--schematic", type=Path, default=None,
                          help="reference schematic (default: verify.toml [cell].schematic, "
                               "else the single schematic in the directory)")
    p_check.add_argument("--source-repository", default=None,
                          help="source repository URL (default: read from git)")
    p_check.add_argument("--source-commit", default=None,
                          help="source commit SHA (default: read from git)")
    p_check.add_argument("--dry-run", "-n", action="store_true", help=_DRY_RUN_HELP)
    p_check.add_argument("--no-aslr-guard", action="store_true", help=_NO_ASLR_HELP)
    p_check.add_argument("--no-cache", action="store_true", help=_NO_CACHE_HELP)
    p_check.add_argument("--resume", action="store_true",
                          help="resume an output directory left interrupted or incomplete")
    p_check.add_argument("--force", action="store_true",
                          help="reuse an output directory produced for another provenance")
    p_check.add_argument("--with-klayout", action="store_true", help=_KLAYOUT_HELP)
    p_check.add_argument("--klayout-tech", default=None, help=_KLAYOUT_TECH_HELP)
    p_check.add_argument("--json", action="store_true", help=_JSON_HELP)

    p_batch = sub.add_parser(
        "batch", help="check each cell in order; report each result",
        description="Run cell checks in order; report each verdict and the highest exit code.",
        epilog=(
            "Example:\n"
            "  sky130-verify batch \"$CELL_DIR\" --pdk-root \"$PDK_ROOT\" "
            "--pdk-commit \"$PDK_COMMIT\"\n\n"
            "Each target must be a complete electrical cell; it is checked exactly as by "
            "`check`. The batch exit code is the highest individual exit code.\n\n"
            f"{_EXIT_CODE_HELP}"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_batch.add_argument("targets", type=Path, nargs="+", help="one or more cell directories")
    p_batch.add_argument("--out-root", type=Path, default=None,
                          help="root output directory, one subdirectory per cell "
                               "(default: the per-cell default of `check`)")
    p_batch.add_argument("--pdk-root", default=None, help=_PDK_ROOT_HELP)
    p_batch.add_argument("--pdk-variant", default=None,
                          help="PDK variant (default: verify.toml [pdk].variant of each cell, else sky130A)")
    p_batch.add_argument("--pdk-commit", default=None, help=_PDK_COMMIT_HELP)
    p_batch.add_argument("--dry-run", "-n", action="store_true", help=_DRY_RUN_HELP)
    p_batch.add_argument("--no-aslr-guard", action="store_true", help=_NO_ASLR_HELP)
    p_batch.add_argument("--no-cache", action="store_true", help=_NO_CACHE_HELP)
    p_batch.add_argument("--resume", action="store_true",
                          help="resume output directories left interrupted or incomplete")
    p_batch.add_argument("--force", action="store_true",
                          help="reuse output directories produced for another provenance")
    p_batch.add_argument("--fail-fast", action="store_true",
                          help="stop at the first non-zero exit code")
    p_batch.add_argument("--with-klayout", action="store_true", help=_KLAYOUT_HELP)
    p_batch.add_argument("--klayout-tech", default=None, help=_KLAYOUT_TECH_HELP)
    p_batch.add_argument("--json", action="store_true", help=_JSON_HELP)

    p_manifest = sub.add_parser(
        "manifest", help="validate a manifest or show its verdicts",
        description="Validate a manifest or display its verdicts.")
    manifest_sub = p_manifest.add_subparsers(dest="manifest_command", required=True,
                                              parser_class=_CliArgumentParser,
                                              title="commands", metavar="<command>")
    p_mvalidate = manifest_sub.add_parser(
        "validate", help="check schema and optional file hashes; report errors",
        description="Check the manifest schema and optional file hashes; report errors.")
    p_mvalidate.add_argument("manifest", type=Path, help="path to manifest.json")
    p_mvalidate.add_argument("--verify-files", type=Path, metavar="ROOT",
                             help="also recompute the SHA-256 of every attested input and artifact "
                                  "under ROOT; runs no EDA tool")
    p_mvalidate.add_argument("--json", action="store_true", help=_JSON_HELP)
    p_mshow = manifest_sub.add_parser(
        "show", help="read a valid manifest; print Markdown or JSON",
        description="Read a valid manifest; print verdicts as Markdown or the full JSON.")
    p_mshow.add_argument("manifest", type=Path, help="path to manifest.json")
    p_mshow.add_argument("--json", action="store_true", help=_JSON_HELP)

    p_badge = sub.add_parser(
        "badge", help="render badge files from a valid manifest",
        description="Render badge files from a validated manifest.")
    badge_sub = p_badge.add_subparsers(dest="badge_command", required=True,
                                        parser_class=_CliArgumentParser,
                                        title="commands", metavar="<command>")
    p_brender = badge_sub.add_parser(
        "render", help="validate a manifest; write badge.svg, badge.json and snippet.md",
        description="Validate a manifest; write SVG, JSON, and Markdown badge files.")
    p_brender.add_argument("manifest", type=Path, help="path to manifest.json")
    p_brender.add_argument("--out", type=Path, default=None,
                           help="output directory (default: the manifest's directory)")
    p_brender.add_argument("--json", action="store_true", help=_JSON_HELP)

    return parser


def _cmd_doctor(args: argparse.Namespace) -> int:
    validate_git_sha(args.pdk_commit, "--pdk-commit")
    report = doctor.run_doctor(args.pdk_root, args.pdk_variant, args.pdk_commit, args.klayout_tech)
    code = exitcodes.OK if report.ok else exitcodes.ENVIRONMENT_INCOMPLETE
    if args.json:
        status = _status_for_exit(code)
        message = "environment ready for check" if report.ok else "environment incomplete"
        print(json.dumps(_json_payload(code, status, message, report=report.to_dict()),
                         ensure_ascii=False, indent=2))
    else:
        print(doctor.format_report_human(report))
    return code


def _cmd_check(args: argparse.Namespace) -> int:
    outcome = run_check(
        target=args.target,
        out_dir=args.out,
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
        print(outcome.message, file=sys.stderr if outcome.exit_code in
              (exitcodes.USAGE_ERROR, exitcodes.ENVIRONMENT_INCOMPLETE, exitcodes.TOOL_FAILURE)
              else sys.stdout)
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
                                       f"checked {len(outcome.results)} cell(s); exit code {outcome.exit_code}",
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
        input_error = _manifest_input_error(args.manifest)
        if input_error is None and args.verify_files is not None and not args.verify_files.is_dir():
            input_error = (f"--verify-files root {args.verify_files} is not a directory; "
                           "pass the directory containing the paths recorded in the manifest")
        if input_error:
            if args.json:
                print(json.dumps(_json_payload(exitcodes.USAGE_ERROR, "usage_error", input_error,
                                               valid=False, errors=[input_error], checked_files=0)))
            else:
                print(input_error, file=sys.stderr)
            return exitcodes.USAGE_ERROR
        ok, errors = validate_manifest_file(args.manifest)
        checked_files = 0
        if ok and args.verify_files is not None:
            ok, errors, checked_files = verify_manifest_files(args.manifest, args.verify_files)
        code = exitcodes.OK if ok else exitcodes.NOT_CLEAN
        if getattr(args, "json", False):
            print(json.dumps(_json_payload(code, "ok" if ok else "not_clean",
                                           f"manifest {args.manifest}: valid" if ok else
                                           f"manifest {args.manifest}: {len(errors)} validation error(s); inspect errors",
                                           valid=ok, errors=errors, checked_files=checked_files),
                             ensure_ascii=False, indent=2))
        elif ok:
            print(f"valid: {args.manifest}")
        else:
            for err in errors:
                print(err, file=sys.stderr)
        return code
    if args.manifest_command == "show":
        input_error = _manifest_input_error(args.manifest)
        if input_error:
            if args.json:
                print(json.dumps(_json_payload(exitcodes.USAGE_ERROR, "usage_error", input_error,
                                               manifest=None)))
            else:
                print(input_error, file=sys.stderr)
            return exitcodes.USAGE_ERROR
        ok, errors = validate_manifest_file(args.manifest)
        if not ok:
            message = f"manifest {args.manifest} is invalid; run 'sky130-verify manifest validate {args.manifest}'"
            if args.json:
                print(json.dumps(_json_payload(exitcodes.NOT_CLEAN, "not_clean", message,
                                               errors=errors, manifest=None)))
            else:
                print(message, file=sys.stderr)
                for error in errors:
                    print(error, file=sys.stderr)
            return exitcodes.NOT_CLEAN
        if args.json:
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            print(json.dumps(_json_payload(exitcodes.OK, "ok", "manifest read",
                                           manifest=manifest), ensure_ascii=False, indent=2))
        else:
            print(render_manifest_markdown(args.manifest))
        return exitcodes.OK
    raise AssertionError("unknown manifest subcommand")


def _cmd_badge(args: argparse.Namespace) -> int:
    if args.badge_command != "render":
        raise AssertionError("unknown badge subcommand")
    input_error = _manifest_input_error(args.manifest)
    if input_error:
        if args.json:
            print(json.dumps(_json_payload(exitcodes.USAGE_ERROR, "usage_error", input_error,
                                           output_dir=None)))
        else:
            print(input_error, file=sys.stderr)
        return exitcodes.USAGE_ERROR
    ok, errors = validate_manifest_file(args.manifest)
    if not ok:
        if args.json:
            print(json.dumps(_json_payload(exitcodes.NOT_CLEAN, "not_clean",
                                           f"manifest {args.manifest} is invalid; inspect errors",
                                           errors=errors, output_dir=None), ensure_ascii=False, indent=2))
        else:
            print(f"manifest {args.manifest} is invalid; run 'sky130-verify manifest validate {args.manifest}'",
                  file=sys.stderr)
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
        print(f"badge written to {out_dir}")
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
            print(json.dumps(_json_payload(exitcodes.TOOL_FAILURE, "tool_failure",
                                           f"I/O failure: {exc}; check file paths and permissions"),
                             ensure_ascii=False, indent=2))
        else:
            print(f"I/O failure: {exc}; check file paths and permissions", file=sys.stderr)
        return exitcodes.TOOL_FAILURE
    except KeyboardInterrupt:
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
