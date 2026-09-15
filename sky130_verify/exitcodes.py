"""Exit codes — single source of truth for every subcommand."""

from __future__ import annotations

OK = 0
NOT_CLEAN = 1
USAGE_ERROR = 2
ENVIRONMENT_INCOMPLETE = 3
TOOL_FAILURE = 4
OUT_OF_SCOPE = 5
