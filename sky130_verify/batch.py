"""``sky130-verify batch`` — several independent cells, one invocation.

Each argument stays a complete electrical cell, verified by the same
:func:`sky130_verify.check.run_check` as a standalone ``check``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import exitcodes
from .check import CheckOutcome, run_check


@dataclass(frozen=True)
class BatchOutcome:
    exit_code: int
    results: tuple[tuple[str, CheckOutcome], ...]  # (label, outcome), in the given order

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "cells": [
                {"target": label, **outcome.to_json_dict()}
                for label, outcome in self.results
            ],
        }


def _unique_labels(targets: tuple[Path, ...]) -> list[str]:
    """Output subdirectory name per cell, disambiguated when two targets
    share a base name."""
    counts: dict[str, int] = {}
    for target in targets:
        counts[target.name] = counts.get(target.name, 0) + 1
    seen: dict[str, int] = {}
    labels = []
    for target in targets:
        if counts[target.name] == 1:
            labels.append(target.name)
        else:
            seen[target.name] = seen.get(target.name, 0) + 1
            labels.append(f"{target.name}-{seen[target.name]}")
    return labels


def run_batch(
    *,
    targets: tuple[Path, ...],
    out_root: Path | None,
    pdk_root: str | None,
    pdk_variant: str | None,
    pdk_commit: str | None,
    dry_run: bool,
    disable_aslr_guard: bool,
    use_cache: bool,
    fail_fast: bool,
    resume: bool = False,
    force: bool = False,
    with_klayout: bool = False,
    klayout_tech: str | None = None,
) -> BatchOutcome:
    """Overall exit code is the worst of the individual codes."""
    labels = _unique_labels(targets)
    results: list[tuple[str, CheckOutcome]] = []
    worst = exitcodes.OK
    for target, label in zip(targets, labels):
        outcome = run_check(
            target=target,
            out_dir=(out_root / label) if out_root is not None else None,
            pdk_root=pdk_root, pdk_variant=pdk_variant, pdk_commit=pdk_commit,
            cell_override=None, schematic_override=None,
            source_repository=None, source_commit=None,
            dry_run=dry_run, disable_aslr_guard=disable_aslr_guard, use_cache=use_cache,
            resume=resume, force=force,
            with_klayout=with_klayout, klayout_tech=klayout_tech,
        )
        results.append((label, outcome))
        worst = max(worst, outcome.exit_code)
        if fail_fast and outcome.exit_code != exitcodes.OK:
            break
    return BatchOutcome(exit_code=worst, results=tuple(results))
