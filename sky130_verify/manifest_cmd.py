"""Validate manifest data and render its verdicts."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path

import jsonschema

from .manifest_backend import MANIFEST_JSON_SCHEMA


def validate_manifest_file(path: Path) -> tuple[bool, list[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, [f"cannot read JSON manifest {path}: {exc}; pass a UTF-8 manifest.json"]

    validator = jsonschema.Draft202012Validator(MANIFEST_JSON_SCHEMA)
    errors = sorted(validator.iter_errors(data), key=lambda e: tuple(str(p) for p in e.path))
    if errors:
        return False, [f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors]
    return True, []


def verify_manifest_files(path: Path, root: Path) -> tuple[bool, list[str], int]:
    """Compare input and artifact hashes with files under ``root``."""
    ok, errors = validate_manifest_file(path)
    if not ok:
        return False, errors, 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:  # file changed since validation
        return False, [f"cannot read JSON manifest {path}: {exc}; pass a UTF-8 manifest.json"], 0

    root_resolved = root.resolve()
    checked = 0
    file_errors: list[str] = []
    for section in ("inputs", "artifacts"):
        for relative_path, expected_digest in data[section].items():
            candidate = root / relative_path
            try:
                # Reject symlinks that leave the supplied root.
                candidate.resolve().relative_to(root_resolved)
            except ValueError:
                file_errors.append(f"{section}/{relative_path}: resolves outside {root}; use the correct --verify-files root")
                continue
            if not candidate.is_file():
                file_errors.append(f"{section}/{relative_path}: missing under {root}; restore the file or use the correct --verify-files root")
                continue
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
            checked += 1
            if digest != expected_digest:
                file_errors.append(
                    f"{section}/{relative_path}: SHA-256 mismatch "
                    f"(expected {expected_digest}, got {digest}); restore the attested file"
                )
    return not file_errors, file_errors, checked


def render_manifest_markdown(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    verification = data.get("verification", {})
    lines = [
        f"# Manifest: {data.get('cell_id', '<unknown>')}",
        "",
        f"- schema_version: {data.get('schema_version')}",
        f"- source: {data.get('source', {}).get('repository')} @ {data.get('source', {}).get('commit')}",
        f"- PDK: {data.get('pdk', {}).get('family')} {data.get('pdk', {}).get('variant')} "
        f"({data.get('pdk', {}).get('commit_sha')})",
        "",
        "| Check | Verdict |",
        "|---|---|",
        f"| DRC | {verification.get('drc_verdict')} |",
        f"| LVS | {verification.get('lvs_verdict')} |",
    ]
    return "\n".join(lines) + "\n"
