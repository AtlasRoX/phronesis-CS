"""
One data root for Phronesis state: PHRONESIS_DATA_DIR.

When PHRONESIS_DATA_DIR is set, every state directory defaults to a subdirectory of it:

  memory/           clinical memory vault (PHRONESIS_MEMORY_DIR wins)
  runs/             run manifests (PHRONESIS_RUNS_DIR wins)
  uploads/          uploaded datasets (PHRONESIS_UPLOAD_ROOT wins)
  jobs/             background-job SQLite store (PHRONESIS_JOBS_DIR, then <PHRONESIS_RUNS_DIR>/jobs, win)
  tool_cache/       tool-result SQLite cache (PHRONESIS_TOOL_CACHE_DIR, then <PHRONESIS_RUNS_DIR>/tool_cache, win)
  biomodels_cache/  downloaded BioModels / COPASI files (default: the system temp directory)

A specific variable always wins over the data root. With nothing set, every store keeps its historical
default (`<workspace>/.phronesis_memory`, `.phronesis_runs`, `.phronesis_uploads`, `data/jobs`, ...).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

DATA_DIR_ENV = "PHRONESIS_DATA_DIR"

# <workspace> = the repository root (phronesis/utils/paths.py -> parents[2]).
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]


def data_root() -> Optional[Path]:
    """The configured data root (PHRONESIS_DATA_DIR, `~` expanded), or None when it is not set."""
    raw = os.getenv(DATA_DIR_ENV, "").strip()
    return Path(os.path.expanduser(raw)) if raw else None


def data_dir(sub: str) -> Optional[Path]:
    """`<PHRONESIS_DATA_DIR>/<sub>`, or None when PHRONESIS_DATA_DIR is not set (callers keep their default)."""
    root = data_root()
    return root / sub if root is not None else None
