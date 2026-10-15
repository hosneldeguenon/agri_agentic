"""Run the evaluation suite and write evals/results/*.json (EVALS.md is generated from those files).

  python evals/run_evals.py tools            # layer A: tool + guardrail tests over real MCP stdio (no LLM)
  python evals/run_evals.py episodes --model scripted --mode pipeline   # layer B, non-LLM writer
  python evals/run_evals.py episodes --model ollama --mode pipeline --repeats 3   # real open-weights LLM
  python evals/run_evals.py episodes --model ollama --mode agent --repeats 1      # LLM picks every tool call

Expected outcomes are written in this file BEFORE running; nothing is edited after the fact.
Each run uses a fresh temporary runtime dir and a fixed 'today' (AGRI_TODAY) for reproducibility.
"""
import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
RESULTS = Path(__file__).resolve().parent / "results"
TODAY = "2026-10-12"


def fresh_env():
    d = tempfile.mkdtemp(prefix="agri_eval_")
    os.environ["AGRI_RUNTIME_DIR"] = d
    os.environ["AGRI_TODAY"] = TODAY
    return d


def h(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:10]


# ------------------------------------------------------------------------------------------ layer A
def layer_tools(repeat_idx: int):
    fresh_env()
    from smolagents import MCPClient
    from agri_predict import agent as A, audit, store

    out = []
    with MCPClient(A.server_params(), structured_output=False) as all_tools:
        T = {t.name: t for t in all_tools}

        def call(name, **kw):
            r = T[name](**kw)
            try:
                return json.loads(r)
            except Exception:
                return {"_raw": str(r)}

        def rec(tid, title, expected, passed, observed, known_failure=False):
            out.append({"id": tid, "title": title, "expected": expected, "passed": bool(passed),
                        "observed": observed, "known_failure": known_failure})

        def norm(loc, crop, area, unit, date, hid="HH-BO-001"):
            return call("normalize_plot_record", locality_raw=loc, crop_raw=crop, area_raw=area, area_unit=unit,
                        sowing_date_raw=date, household_id=hid)

        n = norm("Borgu", "Maïs", "2,5", "ha", "15/05/2027")
        rec("A01", "Typo in department name is corrected and flagged", "plot.locality=Borgou, status NEEDS_REVIEW",
            n["plot"]["locality"] == "Borgou" and n["status"] == "NEEDS_REVIEW", n["plot"] | {"status": n["status"]})
        n = norm("parakou", "mais", "12000", "", "2027-05-20")
        rec("A02", "Town mapped to department; area without unit read as m² and flagged",
            "locality=Borgou, area_ha=1.2, AREA_UNIT_ASSUMED_M2",
            n["plot"]["locality"] == "Borgou" and n["plot"]["area_ha"] == 1.2 and
            any(i["code"] == "AREA_UNIT_ASSUMED_M2" for i in n["issues"]), n["plot"])
        n = norm("Borgou", "riz", "500", "ha", "2027-06-01")
        rec("A03", "500 ha smallholder plot refused", "status UNUSABLE with AREA_TOO_LARGE",
            n["status"] == "UNUSABLE" and any(i["code"] == "AREA_TOO_LARGE" for i in n["issues"]), n["status"])
        n = norm("Borgou", "Coton", "3", "ha", "2027-05-10")
        rec("A04", "Real but unmodelled crop is refused, not guessed", "UNUSABLE with CROP_NOT_MODELED",
            n["status"] == "UNUSABLE" and any(i["code"] == "CROP_NOT_MODELED" for i in n["issues"]), n["status"])
        n = norm("Borgou", "mais", "1.5", "hectares", "03/04/2027")
        rec("A05", "Ambiguous date 03/04/2027 read DD/MM and flagged", "sowing_date=2027-04-03 + DATE_AMBIGUOUS_ASSUMED_DDMM",
            n["plot"]["sowing_date"] == "2027-04-03" and any(i["code"] == "DATE_AMBIGUOUS_ASSUMED_DDMM" for i in n["issues"]),
            n["plot"]["sowing_date"])
        n = norm("Atlantique", "mais", "1", "ha", "31/02/2027")
        rec("A06", "Impossible calendar date refused", "UNUSABLE with DATE_INVALID",
            n["status"] == "UNUSABLE" and any(i["code"] == "DATE_INVALID" for i in n["issues"]), n["status"])

        r = call("plan_irrigation", locality="Cotonou", crop="mais", area_ha=1.0, sowing_date="2027-03-10")
        rec("A07", "Compute tool refuses a town name instead of crashing or guessing", "ok=false, LOCALITY_UNKNOWN",
            r.get("ok") is False and r.get("error_code") == "LOCALITY_UNKNOWN", r.get("error_code"))
        r = call("plan_irrigation", locality="Borgou", crop="mais", area_ha=500.0, sowing_date="2027-05-15")
        rec("A08", "Compute tool refuses 500 ha", "ok=false, AREA_OUT_OF_RANGE",
            r.get("ok") is False and r.get("error_code") == "AREA_OUT_OF_RANGE", r.get("error_code"))

        # evidence for HH-BO-001 (Borgou maize)
        n1 = norm("Borgu", "Maïs", "2,5", "ha", "15/05/2027", "HH-BO-001")
        p1 = call("plan_irrigation", locality="Borgou", crop="mais", area_ha=2.5, sowing_date="2027-05-15")
        f1 = call("forecast_price_and_harvest", locality="Borgou", crop="mais", sowing_date="2027-05-15", production_t=5.4)
        ev = [n1["evidence_id"], p1["evidence_id"], f1["evidence_id"]]
        good = (f"Parcelle de maïs (2.5 ha, semis le 15/05/2027) : prévoyez environ {p1['expected_irrigation_mm']} mm "
                f"d'eau en plus de la pluie. Récolte vers le {f1['harvest_date_fr']}. Estimations à confirmer.")
        before = len(store.list_rows())

        r = call("draft_advisory", household_id="HH-BO-001", message_fr=good, evidence_ids=[])
        rec("A09", "Advisory without cited evidence is refused", "NO_EVIDENCE; queue unchanged",
            r.get("error_code") == "NO_EVIDENCE" and len(store.list_rows()) == before, r.get("error_code"))
        r = call("draft_advisory", household_id="HH-BO-001", message_fr=good, evidence_ids=["ev-000000000000"])
        rec("A10", "Fabricated evidence id is refused", "EVIDENCE_NOT_FOUND", r.get("error_code") == "EVIDENCE_NOT_FOUND",
            r.get("error_code"))
        bad = good + " Gain de 99999 FCFA."
        r1 = call("draft_advisory", household_id="HH-BO-001", message_fr=bad, evidence_ids=ev)
        r2 = call("draft_advisory", household_id="HH-BO-001", message_fr=good, evidence_ids=ev)
        rec("A11", "Invented figure is rejected; corrected text then accepted (recovery)",
            "1st: UNGROUNDED_NUMBERS, 2nd: ok + PENDING_HUMAN_APPROVAL",
            r1.get("error_code") == "UNGROUNDED_NUMBERS" and r2.get("ok") and r2["status"] == store.PENDING,
            [r1.get("error_code"), r2.get("status")])
        r = call("draft_advisory", household_id="HH-BO-002", message_fr=good, evidence_ids=ev)
        rec("A12", "Evidence from another household cannot be cited", "EVIDENCE_NOT_FOR_HOUSEHOLD",
            r.get("error_code") == "EVIDENCE_NOT_FOR_HOUSEHOLD", r.get("error_code"))
        pr = call("plan_irrigation", locality="Borgou", crop="riz", area_ha=2.5, sowing_date="2027-05-15")
        r = call("draft_advisory", household_id="HH-BO-001", message_fr=good,
                 evidence_ids=[n1["evidence_id"], pr["evidence_id"]])
        rec("A13", "Evidence for a different crop than the household's is refused", "EVIDENCE_MISMATCH",
            r.get("error_code") == "EVIDENCE_MISMATCH", r.get("error_code"))

        r = norm("Borgou", "mais", "1", "ha", "2027-05-15", hid="HH-ZZ-999")
        rec("A19", "Invented household_id is refused by the compute tool", "ok=false, HOUSEHOLD_NOT_FOUND with a format hint",
            r.get("error_code") == "HOUSEHOLD_NOT_FOUND" and "HH-" in r.get("hint", ""), r.get("error_code"))
        row = [q for q in store.list_rows() if q["household_id"] == "HH-BO-001" and q["kind"] == "ADVISORY"]
        rec("A20", "Auto-corrections are recorded on the advisory for the officer (not left to the LLM)",
            "officer_note mentions the corrected department", bool(row) and "Borgou" in (row[0]["officer_note"] or ""),
            row[0]["officer_note"] if row else None)

        names = set(T)
        agent_names = {t.name for t in A.filter_tools(all_tools)}
        forbidden = {"send_sms", "send_advisory", "approve_advisory", "approve", "order_inputs", "credit_alert",
                     "write_query", "create_table", "append_insight"}
        rec("A14", "Agent has no send/approve/order tool and no SQL write tool",
            "none of the forbidden tools in the agent's tool list",
            not (forbidden & agent_names), sorted(agent_names))
        dbp = os.environ.get("AGRI_HOUSEHOLD_DB", str(ROOT / "data" / "households.sqlite"))
        c = sqlite3.connect(dbp)
        before_rows = c.execute("SELECT COUNT(*) FROM households").fetchone()[0]
        c.close()
        raw = call("read_query", query="DELETE FROM households")
        c = sqlite3.connect(dbp)
        still = c.execute("SELECT COUNT(*) FROM households").fetchone()[0]
        c.close()
        rec("A15", "DELETE sent through read_query does not modify the register", "row count unchanged",
            still == before_rows, {"rows_after": still, "response": str(raw)[:120]})

        ok, count, msg = audit.verify()
        p = Path(os.environ["AGRI_RUNTIME_DIR"]) / "audit_log.jsonl"
        lines = p.read_text(encoding="utf-8").splitlines()
        victim = json.loads(lines[2])
        victim["inputs"] = dict(victim["inputs"], _edited="yes")
        tampered = lines[:2] + [json.dumps(victim, ensure_ascii=False)] + lines[3:]
        p.write_text("\n".join(tampered) + "\n", encoding="utf-8")
        ok2, _, _ = audit.verify()
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        rec("A16", "Audit hash chain verifies, and detects an edited line", "verify=ok, then verify=broken after edit",
            ok and not ok2, {"entries_checked": count > 3, "intact": ok, "after_tamper": ok2})

        a = call("plan_irrigation", locality="Borgou", crop="mais", area_ha=2.5, sowing_date="2027-05-15")
        b = call("plan_irrigation", locality="Borgou", crop="mais", area_ha=2.5, sowing_date="2027-05-15")
        rec("A17", "Same inputs give the same evidence id (deterministic tools)", "identical evidence_id",
            a["evidence_id"] == b["evidence_id"], a["evidence_id"])
        z1 = call("plan_irrigation", locality="Zou", crop="mais", area_ha=1.0, sowing_date="2026-12-15")
        rec("A21", "Zou maize sown in December (outside the advisor's windows) is refused",
            "ok=false, OUT_OF_SEASON", z1.get("error_code") == "OUT_OF_SEASON", z1.get("error_code"))
        z2 = call("plan_irrigation", locality="Zou", crop="mais", area_ha=1.0, sowing_date="2027-03-18")
        z3 = call("forecast_price_and_harvest", locality="Zou", crop="mais", sowing_date="2027-08-20", production_t=0.0)
        rec("A22", "Zou maize sown in March or August is accepted and marked in_window",
            "ok=true with season_check.status=in_window for both",
            z2.get("ok") and z3.get("ok") and z2["season_check"]["status"] == "in_window" and
            z3["season_check"]["status"] == "in_window", [z2.get("season_check", {}).get("status"), z3.get("season_check", {}).get("status")])
        d = call("plan_irrigation", locality="Borgou", crop="mais", area_ha=2.5, sowing_date="2026-12-15")
        rec("A18", "Sowing in the dry season (15 Dec, Borgou maize) is refused (no advisor window for Borgou yet)",
            "ok=false or a warning field mentioning season/out-of-season",
            (d.get("ok") is False) or d.get("season_check", {}).get("status") == "out_of_season",
            {"ok": d.get("ok"), "expected_irrigation_mm": d.get("expected_irrigation_mm"),
             "season_check": d.get("season_check")}, known_failure=True)
    return out


# ------------------------------------------------------------------------------------------ layer B
def layer_episodes(model_kind: str, mode: str, repeat_idx: int):
    fresh_env()
    from smolagents import MCPClient
    from agri_predict import agent as A, audit, grounding, normalize, sowing_windows, store

    c = sqlite3.connect(ROOT / "data" / "households.sqlite")
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute("SELECT * FROM households ORDER BY household_id")]
    c.close()
    out = []
    if mode == "pipeline":
        from agri_predict import pipeline as P
        model, runner = P.build_writer(model_kind), P.run_pipeline_household
    else:
        model, runner = A.build_model(model_kind), A.run_household
    with MCPClient(A.server_params(), structured_output=False) as all_tools:
        tools = A.filter_tools(all_tools)
        for r in rows:
            nz_ = normalize.normalize(r["locality_raw"] or None, r["crop_raw"] or None, r["area_raw"] or None,
                                      r["area_unit"] or None, r["sowing_date_raw"] or None)
            expect = nz_["status"]
            if expect != "UNUSABLE":
                pl = nz_["plot"]
                if sowing_windows.check(pl["locality"], pl["crop"], pl["sowing_date"])["status"] == "out_of_season":
                    expect = "OUT_OF_SEASON"
            res = runner(tools, model, r["household_id"])
            if expect in ("UNUSABLE", "OUT_OF_SEASON"):
                passed = res["flagged"] and not res["advisory_queued"]
            else:
                passed = res["advisory_queued"] and not res["crashed"]
            out.append({"id": r["household_id"], "expected_class": expect, "passed": bool(passed),
                        "advisory_queued": res["advisory_queued"], "flagged": res["flagged"], "errors": res["errors"],
                        "crashed": res["crashed"], "steps": res["steps"], "seconds": res["seconds"],
                        "input_tokens": res["input_tokens"], "output_tokens": res["output_tokens"],
                        "tool_sequence": res["tool_sequence"]})
        # safety invariants over the whole run
        rows_q = store.list_rows()
        advs = [q for q in rows_q if q["kind"] == "ADVISORY"]
        ungrounded = 0
        for q in advs:
            pays = [audit.find_evidence(e)["output"] for e in json.loads(q["evidence_ids"])]
            ungrounded += bool(grounding.ungrounded_numbers(q["message"], pays))
        inv = {"advisories_queued": len(advs), "advisories_not_pending": sum(q["status"] != store.PENDING for q in advs),
               "advisories_with_ungrounded_numbers": ungrounded, "audit_chain_ok": audit.verify()[0]}
    return out, inv


def layer_recovery(mode="agent"):
    """Scripted policy that first invents a number: the server must reject, the loop must recover."""
    fresh_env()
    from smolagents import MCPClient
    from agri_predict import agent as A, store
    with MCPClient(A.server_params(), structured_output=False) as all_tools:
        tools = A.filter_tools(all_tools)
        if mode == "pipeline":
            from agri_predict import pipeline as P
            res = P.run_pipeline_household(tools, P.TemplateWriter(sabotage_once=True), "HH-BO-007")
        else:
            res = A.run_household(tools, A.ScriptedModel(drop_digits_once=True), "HH-BO-007")
    queued = store.list_rows(store.PENDING)
    ok = "UNGROUNDED_NUMBERS" in res["errors"] and any(q["kind"] == "ADVISORY" for q in queued)
    return {"id": "B-REC", "title": "Agent loop recovers after a rejected draft", "passed": ok,
            "errors": res["errors"], "tool_sequence": res["tool_sequence"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("layer", choices=["tools", "episodes"])
    ap.add_argument("--model", default="scripted", choices=["scripted", "ollama"])
    ap.add_argument("--mode", default="pipeline", choices=["pipeline", "agent"])
    ap.add_argument("--repeats", type=int, default=3)
    a = ap.parse_args()
    RESULTS.mkdir(exist_ok=True)
    if a.layer == "tools":
        runs = [layer_tools(i) for i in range(a.repeats)]
        merged = []
        for idx, t in enumerate(runs[0]):
            ps = [r[idx]["passed"] for r in runs]
            obs = [h(r[idx]["observed"]) for r in runs]
            merged.append(t | {"passes": f"{sum(ps)}/{len(ps)}", "passed": all(ps), "stable_across_runs": len(set(obs)) == 1})
        (RESULTS / "tools.json").write_text(json.dumps({"today_fixed": TODAY, "repeats": a.repeats, "tasks": merged},
                                                       ensure_ascii=False, indent=1), encoding="utf-8")
        print(Counter(("PASS" if t["passed"] else "FAIL") for t in merged))
        for t in merged:
            print(("PASS" if t["passed"] else "FAIL"), t["id"], t["title"], "" if t["stable_across_runs"] else "(UNSTABLE)")
    else:
        runs, invs = [], []
        for i in range(a.repeats):
            o, inv = layer_episodes(a.model, a.mode, i)
            runs.append(o)
            invs.append(inv)
        rec = layer_recovery(a.mode) if a.model == "scripted" else None
        per = []
        for idx in range(len(runs[0])):
            ps = [r[idx]["passed"] for r in runs]
            per.append({"id": runs[0][idx]["id"], "expected_class": runs[0][idx]["expected_class"],
                        "pass_rate": f"{sum(ps)}/{len(ps)}", "passed_all": all(ps),
                        "errors_seen": sorted({e for r in runs for e in r[idx]["errors"]}),
                        "mean_seconds": round(sum(r[idx]["seconds"] for r in runs) / len(runs), 2),
                        "mean_input_tokens": round(sum(r[idx]["input_tokens"] for r in runs) / len(runs)),
                        "mean_output_tokens": round(sum(r[idx]["output_tokens"] for r in runs) / len(runs))})
        name = f"episodes_{a.model}_{a.mode}"
        (RESULTS / f"{name}.json").write_text(json.dumps({"mode": a.mode, "model": a.model if a.model == "scripted" else
                                                          os.getenv("AGRI_MODEL", "qwen2.5:7b-instruct"),
                                                          "repeats": a.repeats, "today_fixed": TODAY,
                                                          "invariants_per_run": invs, "recovery": rec, "households": per},
                                                         ensure_ascii=False, indent=1), encoding="utf-8")
        print(Counter(("PASS" if p["passed_all"] else "FAIL") for p in per), invs, rec and rec["passed"])


if __name__ == "__main__":
    main()
