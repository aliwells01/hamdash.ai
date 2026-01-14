# db_paths.py
from __future__ import annotations
import os
from pathlib import Path

def repo_root() -> Path:
    # repo root = directory containing this file
    return Path(__file__).resolve().parent

def spots_db_path() -> Path:
    """
    Returns the path to the SQLite DB file.
    Priority:
      1) SPOTS_DB env var (absolute or relative)
      2) <repo_root>/data/spots.sqlite
    """
    env = os.getenv("SPOTS_DB")
    if env:
        p = Path(env)
        return p if p.is_absolute() else (repo_root() / p)
    return repo_root() / "data" / "spots.sqlite"
