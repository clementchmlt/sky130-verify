"""``verify.toml`` — explicit declaration of a cell's inputs, for cases
where automatic discovery under the cell directory is ambiguous.

Precedence: an explicit CLI flag wins over ``verify.toml``, which wins
over automatic discovery.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

CONFIG_FILENAME = "verify.toml"


class VerifyConfigError(Exception):
    """``verify.toml`` present but invalid — exit code 2."""


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
    """Loads and validates ``verify.toml``. ``layout``/``schematic`` paths
    are resolved relative to ``base_dir``, not the process cwd."""
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
            f"{path}: invalid [cell]"
            + (f" — unknown key(s): {', '.join(sorted(extra))}" if extra else "")
        )
    pdk = data.get("pdk", {})
    if not isinstance(pdk, dict) or set(pdk) - _KNOWN_PDK_KEYS:
        raise VerifyConfigError(f"{path}: invalid [pdk]")
    source = data.get("source", {})
    if not isinstance(source, dict) or set(source) - _KNOWN_SOURCE_KEYS:
        raise VerifyConfigError(f"{path}: invalid [source]")

    def _resolve_path(value: object, field: str) -> Path | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise VerifyConfigError(f"{path}: {field} must be a non-empty string")
        resolved = (base_dir / value).resolve()
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
