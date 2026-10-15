"""Tamper-evident audit log (JSON Lines, SHA-256 hash chain).

Every MCP tool call and every human decision is one line:
  seq, ts (UTC), event, actor, tool, inputs, output, evidence_id, prev_hash, hash
`verify()` recomputes the chain: editing or deleting a line breaks it.
This is tamper-*evident*, not tamper-proof (someone with disk access can rewrite
the whole file); see ARCHITECTURE.md for what would be needed in production.
"""
import contextlib
import hashlib
import json
from datetime import datetime, timezone

from .config import runtime_dir

GENESIS = "0" * 64

try:  # Linux / macOS
    import fcntl

    def _lock_file(f):
        fcntl.flock(f, fcntl.LOCK_EX)

    def _unlock_file(f):
        fcntl.flock(f, fcntl.LOCK_UN)
except ImportError:  # Windows
    import msvcrt

    def _lock_file(f):
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock_file(f):
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


@contextlib.contextmanager
def _locked():
    """Cross-process lock on a side file so the MCP server and review.py never interleave writes."""
    with open(runtime_dir() / "audit_log.lock", "a+b") as lf:
        if lf.seek(0, 2) == 0:
            lf.write(b"0")
            lf.flush()
        _lock_file(lf)
        try:
            yield
        finally:
            _unlock_file(lf)


def _path():
    return runtime_dir() / "audit_log.jsonl"


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))


def _hash(rec: dict) -> str:
    body = {k: v for k, v in rec.items() if k != "hash"}
    return hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()


def evidence_id(tool: str, inputs: dict, output: dict) -> str:
    return "ev-" + hashlib.sha256(canonical([tool, inputs, output]).encode("utf-8")).hexdigest()[:12]


def append(event: str, actor: str, tool: str | None, inputs: dict, output: dict,
           ev_id: str | None = None) -> dict:
    p = _path()
    with _locked():
        p.touch(exist_ok=True)
        with open(p, "r+", encoding="utf-8") as f:
            lines = [l for l in f.read().splitlines() if l.strip()]
            prev, seq = (GENESIS, 0)
            if lines:
                last = json.loads(lines[-1])
                prev, seq = last["hash"], last["seq"] + 1
            rec = {"seq": seq, "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                   "event": event, "actor": actor, "tool": tool, "inputs": inputs,
                   "output": output, "evidence_id": ev_id, "prev_hash": prev}
            rec["hash"] = _hash(rec)
            f.seek(0, 2)
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    return rec


def read_all() -> list[dict]:
    p = _path()
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def verify() -> tuple[bool, int, str]:
    prev, n = GENESIS, 0
    for rec in read_all():
        if rec["prev_hash"] != prev or rec["hash"] != _hash(rec) or rec["seq"] != n:
            return False, n, f"chain broken at seq {rec.get('seq')}"
        prev, n = rec["hash"], n + 1
    return True, n, "ok"


def find_evidence(ev_id: str) -> dict | None:
    """Return the logged successful tool call that issued this evidence id."""
    for rec in read_all():
        if rec.get("evidence_id") == ev_id and rec["event"] == "tool_call":
            return rec
    return None
