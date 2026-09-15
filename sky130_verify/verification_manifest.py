"""Serializable contract for the eight-field per-cell manifest."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path, PurePosixPath
import re
from collections.abc import Mapping
from typing import Any


SCHEMA_VERSION = "1.0.0"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40,64}$")
_VERDICTS = frozenset({"pass", "fail", "error", "not_run", "unknown"})


class ManifestValidationError(ValueError):
    """The eight-field publication contract is not satisfied."""


def _relative_path(path: str, field: str) -> str:
    parsed = PurePosixPath(path)
    if not path or parsed.is_absolute() or ".." in parsed.parts or path.startswith("./"):
        raise ManifestValidationError(f"{field} must be a normalized relative path")
    return parsed.as_posix()


def _digest(value: str, field: str) -> str:
    if not _SHA256.fullmatch(value):
        raise ManifestValidationError(f"{field} must be a hex SHA-256 digest")
    return value


def _git_sha(value: str, field: str) -> str:
    if not _GIT_SHA.fullmatch(value):
        raise ManifestValidationError(f"{field} must be a 40-64 hex character Git SHA")
    return value


def _hash_map(value: Mapping[str, str], field: str) -> dict[str, str]:
    if not value:
        raise ManifestValidationError(f"{field} cannot be empty")
    return {_relative_path(path, field): _digest(digest, field) for path, digest in value.items()}


@dataclass(frozen=True)
class Source:
    repository: str
    commit: str
    paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.repository or "://" not in self.repository:
            raise ManifestValidationError("source.repository must be a repository URL")
        _git_sha(self.commit, "source.commit")
        if not self.paths:
            raise ManifestValidationError("source.paths cannot be empty")
        for path in self.paths:
            _relative_path(path, "source.paths")


@dataclass(frozen=True)
class Pdk:
    family: str
    variant: str
    commit_sha: str
    repository: str

    def __post_init__(self) -> None:
        if not self.family or not self.variant or not self.repository:
            raise ManifestValidationError("pdk must name a family, variant, and repository")
        _git_sha(self.commit_sha, "pdk.commit_sha")


@dataclass(frozen=True)
class Verification:
    drc_verdict: str
    lvs_verdict: str
    drc_log: str | None = None
    lvs_log: str | None = None

    def __post_init__(self) -> None:
        if self.drc_verdict not in _VERDICTS or self.lvs_verdict not in _VERDICTS:
            raise ManifestValidationError("verification.drc_verdict and lvs_verdict are a closed enum")
        if self.drc_log is not None:
            _relative_path(self.drc_log, "verification.drc_log")
        if self.lvs_log is not None:
            _relative_path(self.lvs_log, "verification.lvs_log")


@dataclass(frozen=True)
class VerificationManifest:
    """Typed model of the eight required top-level blocks."""

    schema_version: str
    cell_id: str
    source: Source
    pdk: Pdk
    toolchain: Mapping[str, Any]
    inputs: Mapping[str, str]
    verification: Verification
    artifacts: Mapping[str, str]

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ManifestValidationError(f"schema_version must be {SCHEMA_VERSION}")
        if not self.cell_id:
            raise ManifestValidationError("cell_id cannot be empty")
        if not self.toolchain or not isinstance(self.toolchain.get("tools"), Mapping):
            raise ManifestValidationError("toolchain.tools is required")
        _hash_map(self.inputs, "inputs")
        _hash_map(self.artifacts, "artifacts")

    def to_dict(self) -> dict[str, Any]:
        """Serialize the eight fields in schema order."""
        return {
            "schema_version": self.schema_version,
            "cell_id": self.cell_id,
            "source": {
                "repository": self.source.repository,
                "commit": self.source.commit,
                "paths": list(self.source.paths),
            },
            "pdk": asdict(self.pdk),
            "toolchain": dict(self.toolchain),
            "inputs": _hash_map(self.inputs, "inputs"),
            # drc_log/lvs_log: omit when absent rather than emit null.
            "verification": {key: value for key, value in asdict(self.verification).items() if value is not None},
            "artifacts": _hash_map(self.artifacts, "artifacts"),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=False) + "\n"


MANIFEST_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://github.com/clementchmlt/sky130-verify/schema/verification-manifest-1.0.0.json",
    "title": "Sky130 verification manifest",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "cell_id", "source", "pdk", "toolchain", "inputs", "verification", "artifacts"],
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "cell_id": {"type": "string", "minLength": 1},
        "source": {
            "type": "object", "additionalProperties": False,
            "required": ["repository", "commit", "paths"],
            "properties": {
                "repository": {"type": "string", "format": "uri"},
                "commit": {"type": "string", "pattern": "^[0-9a-f]{40,64}$"},
                "paths": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/relative_path"}},
            },
        },
        "pdk": {
            "type": "object", "additionalProperties": False,
            "required": ["family", "variant", "commit_sha", "repository"],
            "properties": {
                "family": {"type": "string", "minLength": 1},
                "variant": {"type": "string", "minLength": 1},
                "commit_sha": {"type": "string", "pattern": "^[0-9a-f]{40,64}$"},
                "repository": {"type": "string", "format": "uri"},
            },
        },
        "toolchain": {
            "type": "object", "additionalProperties": True,
            "required": ["tools"],
            "properties": {"tools": {"type": "object", "minProperties": 1}},
        },
        "inputs": {"$ref": "#/$defs/hash_map"},
        "verification": {
            "type": "object", "additionalProperties": False,
            "required": ["drc_verdict", "lvs_verdict"],
            "properties": {
                "drc_verdict": {"$ref": "#/$defs/verdict"},
                "lvs_verdict": {"$ref": "#/$defs/verdict"},
                "drc_log": {"$ref": "#/$defs/relative_path"},
                "lvs_log": {"$ref": "#/$defs/relative_path"},
            },
        },
        "artifacts": {"$ref": "#/$defs/hash_map"},
    },
    "$defs": {
        "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "relative_path": {"type": "string", "minLength": 1, "pattern": "^(?!/)(?!.*(?:^|/)\\.\\.(?:/|$))(?!\\./).+"},
        "hash_map": {"type": "object", "minProperties": 1,
                     "propertyNames": {"$ref": "#/$defs/relative_path"},
                     "additionalProperties": {"$ref": "#/$defs/sha256"}},
        "verdict": {"enum": sorted(_VERDICTS)},
    },
}


def write_json_schema(path: Path) -> None:
    """Writes the canonical schema with deterministic formatting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(MANIFEST_JSON_SCHEMA, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
