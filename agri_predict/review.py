"""Human approval console for the extension officer. NOT exposed through MCP.

  python -m agri_predict.review list
  python -m agri_predict.review show ADV-0001
  python -m agri_predict.review approve ADV-0001 --officer "A. Officer" [--edit "new text"]
  python -m agri_predict.review reject  ADV-0001 --officer "A. Officer" --note "why"
  python -m agri_predict.review verify-log

Approving writes a human_decision line to the audit log and appends a *simulated* send to
runtime/outbox_simulated.jsonl. No real SMS/USSD gateway is connected in this project.
"""
import argparse
import json
import sys

from . import audit, grounding, store
from .config import runtime_dir


def _evidence_lines(row):
    out = []
    for ev in json.loads(row["evidence_ids"]):
        rec = audit.find_evidence(ev)
        if rec:
            src = (rec["output"].get("sources") or ["-"])[0]
            out.append(f"  {ev}  {rec['tool']:<28} {rec['ts'][:16]}  {src[:70]}")
        else:
            out.append(f"  {ev}  (not found in audit log!)")
    return out


def cmd_show(rid):
    r = store.get(rid)
    if not r:
        print("not found")
        return 1
    print(f"{r['id']}  [{r['kind']}]  household {r['household_id']}  channel={r['channel']}  status={r['status']}")
    print(f"created {r['created_at']}\n\n{r['message']}")
    if r["kind"] == "ADVISORY" and r.get("officer_note"):
        print(f"\nNOTE FOR OFFICER: {r['officer_note']}")
    for ev in json.loads(r["evidence_ids"]):
        rec = audit.find_evidence(ev)
        if rec and rec["tool"] == "normalize_plot_record":
            p = rec["output"]["plot"]
            print(f"\nZone: {p['locality']} | crop: {p['crop']} | sowing: {p['sowing_date']}")
    print("\nEvidence (computed at the time shown; datasets are SIMULATED, see sources):")
    print("\n".join(_evidence_lines(r)) or "  (none)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").add_argument("--all", action="store_true")
    sub.add_parser("show").add_argument("id")
    for name in ("approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("id")
        p.add_argument("--officer", required=True, help="Name of the responsible officer")
        p.add_argument("--note", default="")
        if name == "approve":
            p.add_argument("--edit", help="Replace the message text before sending")
    sub.add_parser("verify-log")
    a = ap.parse_args(argv)

    if a.cmd == "list":
        for r in store.list_rows(None if a.all else store.PENDING):
            print(f"{r['id']}  {r['kind']:<8} {r['household_id']:<11} {r['status']:<24} {r['message'][:70]!r}")
        return 0
    if a.cmd == "show":
        return cmd_show(a.id)
    if a.cmd == "verify-log":
        ok, n, msg = audit.verify()
        print(f"audit log: {n} entries, {msg}")
        return 0 if ok else 2
    r = store.get(a.id)
    if not r or r["status"] != store.PENDING:
        print("not found or already decided")
        return 1
    if a.cmd == "reject":
        store.decide(a.id, store.REJECTED, a.officer, a.note)
        audit.append("human_decision", f"officer:{a.officer}", "review.reject", {"id": a.id, "note": a.note},
                     {"status": store.REJECTED})
        print(f"{a.id} rejected")
        return 0
    final = a.edit or r["message"]
    if r["kind"] == "FLAG":
        store.decide(a.id, store.APPROVED, a.officer, "flag acknowledged")
        audit.append("human_decision", f"officer:{a.officer}", "review.acknowledge", {"id": a.id}, {"status": "ACK"})
        print(f"{a.id} acknowledged")
        return 0
    if a.edit:  # an edited text is re-checked against the evidence
        payloads = [audit.find_evidence(e)["output"] for e in json.loads(r["evidence_ids"]) if audit.find_evidence(e)]
        bad = grounding.ungrounded_numbers(final, payloads)
        if bad:
            print(f"warning: edited text contains numbers not in the evidence: {bad}")
            if input("send anyway? [y/N] ").strip().lower() != "y":
                return 1
    store.decide(a.id, store.APPROVED, a.officer, a.note, final)
    audit.append("human_decision", f"officer:{a.officer}", "review.approve",
                 {"id": a.id, "edited": bool(a.edit), "final_message": final}, {"status": store.APPROVED})
    with open(runtime_dir() / "outbox_simulated.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"advisory_id": a.id, "household_id": r["household_id"], "channel": r["channel"],
                            "message": final, "approved_by": a.officer, "sent_at": store.now(),
                            "SIMULATED": True}, ensure_ascii=False) + "\n")
    print(f"{a.id} approved by {a.officer} -> simulated send written to outbox_simulated.jsonl")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
