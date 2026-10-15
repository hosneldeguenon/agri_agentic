"""Advisory queue (SQLite). The agent can only INSERT rows in state
PENDING_HUMAN_APPROVAL. State changes are made by review.py (human) only."""
import json
import sqlite3
from datetime import datetime, timezone

from .config import runtime_dir

PENDING = "PENDING_HUMAN_APPROVAL"
APPROVED = "APPROVED_SENT_SIMULATED"
REJECTED = "REJECTED"

SCHEMA = """CREATE TABLE IF NOT EXISTS advisories(
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, household_id TEXT NOT NULL, channel TEXT,
  message TEXT NOT NULL, officer_note TEXT, evidence_ids TEXT NOT NULL,
  status TEXT NOT NULL, created_at TEXT NOT NULL,
  decided_by TEXT, decided_at TEXT, decision_note TEXT, final_message TEXT)"""


def _conn():
    c = sqlite3.connect(runtime_dir() / "queue.sqlite")
    c.row_factory = sqlite3.Row
    c.execute(SCHEMA)
    return c


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def insert(kind, household_id, channel, message, officer_note, evidence_ids) -> dict:
    """Idempotent: same kind+household+message+evidence returns the existing row."""
    ev = json.dumps(sorted(evidence_ids))
    with _conn() as c:
        row = c.execute("SELECT * FROM advisories WHERE kind=? AND household_id=? AND message=? "
                        "AND evidence_ids=? AND status=?", (kind, household_id, message, ev, PENDING)).fetchone()
        if row:
            return dict(row) | {"duplicate": True}
        n = c.execute("SELECT COUNT(*) FROM advisories").fetchone()[0] + 1
        rid = f"{'ADV' if kind == 'ADVISORY' else 'FLAG'}-{n:04d}"
        c.execute("INSERT INTO advisories(id,kind,household_id,channel,message,officer_note,evidence_ids,"
                  "status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                  (rid, kind, household_id, channel, message, officer_note, ev, PENDING, now()))
        return dict(c.execute("SELECT * FROM advisories WHERE id=?", (rid,)).fetchone()) | {"duplicate": False}


def list_rows(status=None):
    with _conn() as c:
        q, a = "SELECT * FROM advisories", ()
        if status:
            q, a = q + " WHERE status=?", (status,)
        return [dict(r) for r in c.execute(q + " ORDER BY id", a)]


def get(rid):
    with _conn() as c:
        r = c.execute("SELECT * FROM advisories WHERE id=?", (rid,)).fetchone()
        return dict(r) if r else None


def decide(rid, status, officer, note="", final_message=None):
    with _conn() as c:
        cur = c.execute("UPDATE advisories SET status=?, decided_by=?, decided_at=?, decision_note=?, "
                        "final_message=? WHERE id=? AND status=?",
                        (status, officer, now(), note, final_message, rid, PENDING))
        return cur.rowcount == 1
