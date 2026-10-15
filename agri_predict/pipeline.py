"""Pipeline mode: code-orchestrated graph, open-weights LLM as the drafting + recovery node.

Why this exists: with Qwen2.5-7B the free tool-calling agent failed on every real run (LLM_DEV_LOG.md:
loops, empty arguments, invented ids). A 7B model is unreliable at *choosing and parameterising* tools but good
enough at *writing a short French message from given facts* and *rewriting it after a rejection*.

Graph (all tool calls go through the same MCP servers, audit log and server-side checks as agent mode):
  read row (3rd-party sqlite MCP) -> normalize_plot_record --UNUSABLE--> flag_for_officer
        -> plan_irrigation -> forecast_price_and_harvest -> [LLM drafts SMS] -> draft_advisory
        -> rejected? feed the error back to the LLM, retry (max 3) -> still rejected? flag_for_officer
The LLM never sees tools and cannot send anything; draft_advisory still enforces evidence and grounding.
"""
import json
import os
import time

from . import audit
from .agent import _json, prefetch_row

MAX_DRAFTS = 3
CROP_FR = {"mais": "maïs", "riz": "riz", "igname": "igname", "manioc": "manioc", "tomate": "tomate"}

SYSTEM = ("You write SMS advisories for smallholder farmers in Benin, in simple French. Use ONLY the numbers "
          "given in FACTS; never add, round or compute any other number. At most 450 characters. Write units as m³ "
          "(not m3). Meaning of the facts: season_total_* values are the extra water for the WHOLE crop cycle (from "
          "sowing to harvest); never present them as belonging to one week or to a date range. peak_week_* is the "
          "single week with the highest need. Mention: the season total of extra water, the peak week (its start date "
          "and mm), the harvest date, and the sell-or-store advice. Copy dates exactly as written in FACTS. If "
          "'plan_covers_days' is present, say the water plan only covers that many days of the cycle. End with: "
          "Estimations à confirmer avec votre conseiller. Output only the SMS text: no quotes, no markdown, no "
          "preamble. Never convert between mm and m³: copy each value exactly as it appears in FACTS.")


def build_facts(norm_plot, plan, fc) -> dict:
    y, m, d = norm_plot["sowing_date"].split("-")
    f = {"crop": CROP_FR[norm_plot["crop"]], "area_ha": norm_plot["area_ha"], "sowing_date": f"{d}/{m}/{y}",
         "season_total_extra_water_mm": plan["expected_irrigation_mm"],
         "season_total_extra_water_m3": plan["expected_volume_m3"],
         "peak_week_start": plan["peak_week_start"], "peak_week_extra_water_mm": plan["peak_week_expected_mm"],
         "harvest_date": fc["harvest_date_fr"],
         "expected_price_fcfa_per_kg": fc["price_at_harvest_fcfa_kg"]}
    if not plan["cycle_fully_covered"]:
        f["plan_covers_days"] = plan["planned_days"]
        f["cycle_days"] = plan["cycle_days"]
    if fc["advice"] == "store":
        f["advice"] = f"stocker jusqu'en {fc['best_month'][5:7]}/{fc['best_month'][:4]}"
    else:
        f["advice"] = "vendre à la récolte"
    return f


class TemplateWriter:
    """Non-LLM writer for GPU-less demos and tests. `sabotage_once` appends an invented figure on the first draft."""
    name = "template-writer"

    def __init__(self, sabotage_once=False):
        self.sabotage_once = sabotage_once
        self.usage = (0, 0)

    def __call__(self, facts, feedback=None):
        t = (f"Parcelle de {facts['crop']} ({facts['area_ha']} ha, semis le {facts['sowing_date']}) : prévoyez "
             f"environ {facts['season_total_extra_water_mm']} mm d'eau en plus de la pluie sur toute la saison "
             f"({facts['season_total_extra_water_m3']} m³), surtout la semaine du {facts['peak_week_start']} "
             f"({facts['peak_week_extra_water_mm']} mm). ")
        if "plan_covers_days" in facts:
            t += f"Le plan couvre {facts['plan_covers_days']} jours sur {facts['cycle_days']}. "
        t += (f"Récolte vers le {facts['harvest_date']}, prix prévu {facts['expected_price_fcfa_per_kg']} FCFA/kg ; "
              f"conseil : {facts['advice']}. Estimations à confirmer avec votre conseiller.")
        if self.sabotage_once and feedback is None:
            t += " Gain de 99999 FCFA."
        return t


class OllamaWriter:
    """Open-weights model through Ollama's OpenAI-compatible endpoint."""

    def __init__(self):
        from openai import OpenAI
        self.name = os.getenv("AGRI_MODEL", "qwen2.5:7b-instruct")
        self.client = OpenAI(base_url=os.getenv("OLLAMA_URL", "http://localhost:11434/v1"), api_key="ollama")
        self.usage = (0, 0)

    def __call__(self, facts, feedback=None):
        user = "FACTS:\n" + json.dumps(facts, ensure_ascii=False)
        if feedback:
            user += f"\n\nYour previous draft was rejected by the checker: {feedback}\nRewrite it, using only numbers from FACTS."
        r = self.client.chat.completions.create(
            model=self.name, temperature=0.0,
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}])
        if getattr(r, "usage", None):
            self.usage = (self.usage[0] + r.usage.prompt_tokens, self.usage[1] + r.usage.completion_tokens)
        text = (r.choices[0].message.content or "").strip().strip('"').strip()
        return text.replace("```", "").strip()


def build_writer(kind: str):
    return TemplateWriter() if kind == "scripted" else OllamaWriter()


def run_pipeline_household(tools, writer, hid: str) -> dict:
    T = {t.name: t for t in tools}
    n0 = len(audit.read_all())
    t0 = time.time()
    crashed, llm_calls = None, 0
    u0 = writer.usage
    try:
        row = prefetch_row(tools, hid)
        if row is None:
            raise LookupError(f"{hid} not in register")
        z = lambda k: "" if row.get(k) in (None, "") else str(row[k])
        norm = _json(T["normalize_plot_record"](locality_raw=z("locality_raw"), crop_raw=z("crop_raw"),
                                                area_raw=z("area_raw"), area_unit=z("area_unit"),
                                                sowing_date_raw=z("sowing_date_raw"), household_id=hid))
        if norm.get("status") == "UNUSABLE":
            why = "; ".join(i["message"] for i in norm["issues"] if i["severity"] == "error")
            T["flag_for_officer"](household_id=hid, reason=("Unusable record: " + why)[:400],
                                  evidence_ids=[norm["evidence_id"]])
        else:
            p = norm["plot"]
            plan = _json(T["plan_irrigation"](locality=p["locality"], crop=p["crop"], area_ha=p["area_ha"],
                                              sowing_date=p["sowing_date"]))
            prod = z("expected_production_t").replace(",", ".")
            fc = _json(T["forecast_price_and_harvest"](locality=p["locality"], crop=p["crop"],
                                                       sowing_date=p["sowing_date"],
                                                       production_t=float(prod) if prod else 0.0))
            if not (plan.get("ok") and fc.get("ok")):
                T["flag_for_officer"](household_id=hid, evidence_ids=[norm["evidence_id"]],
                                      reason=f"Compute failed: {plan.get('error_code') or fc.get('error_code')}"[:400])
            else:
                ev = [norm["evidence_id"], plan["evidence_id"], fc["evidence_id"]]
                facts, feedback, ok = build_facts(p, plan, fc), None, False
                for _ in range(MAX_DRAFTS):
                    msg = writer(facts, feedback)
                    llm_calls += 1
                    res = _json(T["draft_advisory"](household_id=hid, message_fr=msg, evidence_ids=ev, channel="sms"))
                    if res.get("ok"):
                        ok = True
                        break
                    allowed = ", ".join(f"{k}={v}" for k, v in facts.items() if isinstance(v, (int, float)) or k.endswith(("date", "start")))
                    feedback = (f"{res.get('error_code')}: {res.get('message')} {res.get('hint', '')} "
                                f"The ONLY valid values are: {allowed}. Copy them exactly, do not convert or scale them.")
                if not ok:
                    T["flag_for_officer"](household_id=hid, evidence_ids=[norm["evidence_id"]],
                                          reason=f"Draft rejected {MAX_DRAFTS} times: {feedback}"[:400])
    except Exception as e:
        crashed = f"{type(e).__name__}: {e}"
    calls = [r for r in audit.read_all()[n0:] if r["event"] == "tool_call"]
    queued = [r["output"] for r in calls if r["tool"] in ("draft_advisory", "flag_for_officer") and r["output"].get("ok")]
    return {"household_id": hid, "answer": "", "crashed": crashed, "seconds": round(time.time() - t0, 2),
            "tool_sequence": [r["tool"] for r in calls],
            "errors": [r["output"].get("error_code") for r in calls if r["output"].get("ok") is False],
            "advisory_queued": any("advisory_id" in q for q in queued), "flagged": any("flag_id" in q for q in queued),
            "input_tokens": writer.usage[0] - u0[0], "output_tokens": writer.usage[1] - u0[1],
            "steps": len(calls) + llm_calls}
