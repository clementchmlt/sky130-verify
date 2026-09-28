"""Load explicit cell, PDK and source settings from ``verify.toml``."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

CONFIG_FILENAME = "verify.toml"


class VerifyConfigError(Exception):
    """``verify.toml`` exists but is invalid: exit code 2 (usage error)."""


@dataclass(frozen=True)
class VerifyConfig:
    layout: Path | None = None
    schematic: Path | None = None
    cell: str | None = None
    pdk_variant: str | None = None
    pdk_commit: str | None = None
    source_repository: str | None = None
    source_commit: str | None = None


_KNOWN_TABLES = {"cell", "pdk", "source"}
_KNOWN_CELL_KEYS = {"layout", "schematic", "name"}
_KNOWN_PDK_KEYS = {"variant", "commit"}
_KNOWN_SOURCE_KEYS = {"repository", "commit"}


def find_verify_config(directory: Path) -> Path | None:
    candidate = directory / CONFIG_FILENAME
    return candidate if candidate.is_file() else None


def load_verify_config(path: Path, *, base_dir: Path) -> VerifyConfig:
    """Resolve cell paths relative to the configuration directory."""
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise VerifyConfigError(f"{path}: invalid TOML: {exc}") from exc

    unknown_tables = set(data) - _KNOWN_TABLES
    if unknown_tables:
        raise VerifyConfigError(
            f"{path}: unknown table(s): {', '.join(sorted(unknown_tables))} "
            f"(expected: {', '.join(sorted(_KNOWN_TABLES))})"
        )

    cell = data.get("cell", {})
    if not isinstance(cell, dict) or set(cell) - _KNOWN_CELL_KEYS:
        extra = set(cell) - _KNOWN_CELL_KEYS if isinstance(cell, dict) else set()
        raise VerifyConfigError(
            f"{path}: invalid [cell] table"
            + (f"; unknown key(s): {', '.join(sorted(extra))}" if extra else "")
        )
    pdk = data.get("pdk", {})
    if not isinstance(pdk, dict) or set(pdk) - _KNOWN_PDK_KEYS:
        extra = set(pdk) - _KNOWN_PDK_KEYS if isinstance(pdk, dict) else set()
        raise VerifyConfigError(f"{path}: invalid [pdk] table" +
                                (f"; unknown key(s): {', '.join(sorted(extra))}" if extra else ""))
    source = data.get("source", {})
    if not isinstance(source, dict) or set(source) - _KNOWN_SOURCE_KEYS:
        extra = set(source) - _KNOWN_SOURCE_KEYS if isinstance(source, dict) else set()
        raise VerifyConfigError(f"{path}: invalid [source] table" +
                                (f"; unknown key(s): {', '.join(sorted(extra))}" if extra else ""))

    for table_name, table in (("cell", cell), ("pdk", pdk), ("source", source)):
        for key, value in table.items():
            if not isinstance(value, str) or not value.strip():
                raise VerifyConfigError(
                    f"{path}: {table_name}.{key} must be a non-empty string; got {value!r}"
                )

    def _resolve_path(value: object, field: str) -> Path | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise VerifyConfigError(f"{path}: {field} must be a non-empty string")
        supplied = Path(value)
        if supplied.is_absolute() or ".." in supplied.parts:
            raise VerifyConfigError(f"{path}: {field} = {value!r} must be relative to {base_dir}")
        resolved = (base_dir / supplied).resolve()
        if not resolved.is_relative_to(base_dir.resolve()):
            raise VerifyConfigError(f"{path}: {field} = {value!r} resolves outside {base_dir}")
        if not resolved.is_file():
            raise VerifyConfigError(f"{path}: {field} = {value!r} not found under {base_dir}")
        return resolved

    return VerifyConfig(
        layout=_resolve_path(cell.get("layout"), "cell.layout"),
        schematic=_resolve_path(cell.get("schematic"), "cell.schematic"),
        cell=cell.get("name"),
        pdk_variant=pdk.get("variant"),
        pdk_commit=pdk.get("commit"),
        source_repository=source.get("repository"),
        source_commit=source.get("commit"),
    )
