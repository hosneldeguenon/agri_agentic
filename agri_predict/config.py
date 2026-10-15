"""Paths and switches shared by the MCP server, the review CLI and the agent."""
import os
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOUSEHOLD_DB = Path(os.environ.get("AGRI_HOUSEHOLD_DB", ROOT / "data" / "households.sqlite"))


def today() -> date:
    """Overridable for reproducible tests (AGRI_TODAY=2026-10-12)."""
    v = os.environ.get("AGRI_TODAY")
    return date.fromisoformat(v) if v else date.today()


def runtime_dir() -> Path:
    """Read from the environment on every call so tests can switch directories."""
    d = Path(os.environ.get("AGRI_RUNTIME_DIR", ROOT / "runtime"))
    d.mkdir(parents=True, exist_ok=True)
    return d
