"""Agri-Predict MCP server (stdio).

Six tools. Four read-only compute tools, two *actions* that can only write to a
human review queue. There is deliberately NO tool to send, approve, order inputs
or raise credit alerts: approval happens in review.py, outside the agent's reach.

Run:  python -m agri_predict.mcp_server
"""
import functools
import inspect
import sqlite3
from datetime import date, timedelta
from typing import Any

import pandas as pd
from mcp.server.fastmcp import FastMCP

from . import audit, engine, grounding, normalize, sowing_windows, store
from .config import HOUSEHOLD_DB, today

mcp = FastMCP(
    "agri-predict",
    instructions=(
        "Agricultural advisory tools for a Beninese extension office. Compute tools return data with an "
        "evidence_id. draft_advisory / flag_for_officer only queue items for a human officer; nothing is "
        "ever sent to a farmer by these tools."))

EVIDENCE_TOOLS = {"normalize_plot_record", "predict_season_rainfall", "plan_irrigation",
                  "forecast_price_and_harvest"}
SRC_RAIN = ("dataset_M1_pluie_saisonniere.csv: SIMULATED seasonal rainfall 1981-2024 by department; "
            "ridge regression on Atlantic SST, ENSO, humidity, previous-season rain")
SRC_IRR = ("FAO-56 single-Kc water balance; ET0 by Hargreaves-Samani from dataset_M3_etc_fao56.csv temperatures "
           "and radiation (SIMULATED); daily rain profiles from dataset_M2_scenarios_journaliers.csv (SIMULATED); "
           "Kc values to be validated, soil parameters in engine.py")
SRC_PRICE = ("dataset_M4_prix_marche.csv: SIMULATED monthly prices 2015-2024; linear trend + month effect, "
             "extrapolated beyond 2024")
LIM_CAL = ("Rain scenarios are indexed from the start of the rainy season, not from the calendar sowing date: "
           "an out-of-season sowing date is NOT detected.")


# Guidance returned with every successful result. Kept free of digits: the grounding check treats every
# number in a tool output as citable evidence.
NEXT_STEP = {
    "normalize_plot_record": "If status is UNUSABLE, call flag_for_officer and stop. Otherwise (OK or NEEDS_REVIEW) "
                             "call plan_irrigation with the values in `plot`. NEEDS_REVIEW only means some values were "
                             "auto-corrected: it is not an error, do NOT call this tool again for the same household.",
    "plan_irrigation": "Next, call forecast_price_and_harvest with the same locality, crop and sowing_date.",
    "forecast_price_and_harvest": "Next, write message_fr using only figures from the outputs you already have, then "
                                  "call draft_advisory with the evidence_id of normalize_plot_record, plan_irrigation "
                                  "and this call.",
    "draft_advisory": "Finish with final_answer. Auto-corrections are recorded for the officer by the server.",
    "flag_for_officer": "Finish with final_answer.",
}
_SEEN: set[str] = set()


HH_HINT = "Use exactly the household_id given in the task (format HH-XX-NNN, for example HH-BO-001). Never invent one."


def err(code: str, message: str, hint: str = "") -> dict:
    return {"ok": False, "error_code": code, "message": message, "hint": hint}


def audited(fn):
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        inputs = dict(bound.arguments)
        try:
            out = fn(**inputs)
        except Exception as e:  # never crash the server on a bad call
            out = err("INTERNAL_ERROR", f"{type(e).__name__}: {e}")
        if out.get("ok") and fn.__name__ in NEXT_STEP:
            out["next_step"] = NEXT_STEP[fn.__name__]
        ev = None
        if out.get("ok") and fn.__name__ in EVIDENCE_TOOLS:
            ev = audit.evidence_id(fn.__name__, inputs, out)
            out["evidence_id"] = ev
        key = audit.canonical([fn.__name__, inputs])
        if out.get("ok") and key in _SEEN:
            out["duplicate_call"] = "You already made this exact call. Reuse the earlier result and follow next_step."
        _SEEN.add(key)
        audit.append("tool_call", "agent", fn.__name__, inputs, out, ev)
        return out
    return wrapper


def _check_plot(locality: str, crop: str, area_ha: float | None, sowing_date: str | None):
    if locality not in engine.localites():
        return err("LOCALITY_UNKNOWN", f"'{locality}' is not a known department.",
                   "Call normalize_plot_record first. Valid: " + ", ".join(engine.localites()))
    if crop not in engine.KC:
        return err("CROP_UNSUPPORTED", f"'{crop}' is not modelled.", "Supported: " + ", ".join(engine.KC))
    if area_ha is not None and not (normalize.MIN_AREA_HA <= area_ha <= normalize.MAX_AREA_HA):
        return err("AREA_OUT_OF_RANGE", f"area_ha={area_ha} outside [{normalize.MIN_AREA_HA}, {normalize.MAX_AREA_HA}].")
    if sowing_date is not None:
        try:
            d = date.fromisoformat(sowing_date)
        except ValueError:
            return err("DATE_FORMAT", f"'{sowing_date}' is not YYYY-MM-DD.", "Use normalize_plot_record output.")
        if d > today() + timedelta(days=400) or d < today() - timedelta(days=200):
            return err("DATE_OUT_OF_RANGE", f"{sowing_date} is more than 200 days past or 400 days ahead.")
    return None


def _fr(iso: str) -> str:
    a, m, j = iso.split("-")
    return f"{j}/{m}/{a}"


# --------------------------------------------------------------------------- compute tools
@mcp.tool()
@audited
def normalize_plot_record(locality_raw: str = "", crop_raw: str = "", area_raw: str = "",
                          area_unit: str = "", sowing_date_raw: str = "",
                          household_id: str = "") -> dict[str, Any]:
    """Clean one messy plot record from an extension-office register.

    Fixes typos in department/crop names, maps towns to departments, converts area to hectares
    (ha, m2, ares, or unit missing), parses dates (ISO, DD/MM/YYYY, '15 mai 2027') and flags
    implausible values. Returns status OK | NEEDS_REVIEW | UNUSABLE, the cleaned `plot`, and a list of
    `issues`. Pass household_id so the evidence can be tied to that household. Pass an empty string for any
    field that is empty in the register (never invent a value).
    """
    if household_id and _household_exists(household_id) is False:
        return err("HOUSEHOLD_NOT_FOUND", f"{household_id} is not in the household register.", HH_HINT)
    r = normalize.normalize(locality_raw or None, crop_raw or None, area_raw or None, area_unit or None,
                            sowing_date_raw or None)
    r["ok"] = True
    r["sources"] = ["deterministic rules in normalize.py (no data source)"]
    return r


@mcp.tool()
@audited
def predict_season_rainfall(locality: str, sst_anomaly: float = 0.0, enso_index: float = 0.0,
                            humidity_pct: float = 45.0) -> dict[str, Any]:
    """Forecast total rainy-season rainfall (mm) for a Beninese department with an 80% interval and a
    dry/normal/wet category. Defaults give an 'average' season. `locality` must be a clean department
    name (see normalize_plot_record)."""
    e = _check_plot(locality, "mais", None, None)
    if e:
        return e
    p = engine.predire_saison(locality, sst_anomaly, enso_index, humidity_pct)
    return {"ok": True, "locality": locality, "season_category": p["categorie"],
            "season_rain_mm": int(p["pluie_prevue_mm"]),
            "interval_80pct_low_mm": int(p["intervalle_80pct_mm"][0]),
            "interval_80pct_high_mm": int(p["intervalle_80pct_mm"][1]),
            "sources": [SRC_RAIN], "limitations": ["Seasonal total only; daily rain cannot be predicted."]}


@mcp.tool()
@audited
def plan_irrigation(locality: str, crop: str, area_ha: float, sowing_date: str) -> dict[str, Any]:
    """FAO-56 crop water balance: irrigation needed (mm and m3) under dry / normal / wet rainy-season
    scenarios, the peak-demand week and the number of irrigation days. `sowing_date` is YYYY-MM-DD.
    Check `cycle_fully_covered`: if false the plan only covers the first `planned_days` of the cycle."""
    e = _check_plot(locality, crop, area_ha, sowing_date)
    if e:
        return e
    sw = sowing_windows.check(locality, crop, sowing_date)
    if sw["status"] == "out_of_season":
        return err("OUT_OF_SEASON", sw["note"], "Do not draft an advisory; flag the household for the officer.")
    r = engine.recommander(locality, crop, area_ha, sowing_date)["resume"]
    irr = engine.plan_irrigation(locality, crop, area_ha, sowing_date)
    weeks = irr["resume_semaine"]
    return {"ok": True, "locality": locality, "crop": crop, "area_ha": area_ha, "sowing_date": sowing_date,
            "cycle_days": r["duree_cycle_jours"], "planned_days": r["jours_planifies_irrigation"],
            "cycle_fully_covered": r["jours_planifies_irrigation"] >= r["duree_cycle_jours"],
            "rain_scenarios": [{"name": {"sèche": "dry", "normale": "normal", "humide": "wet"}[k],
                                "probability": v["probabilite"], "season_rain_mm": v["cumul_mm"],
                                "irrigation_mm": int(irr["total_mm"][k])} for k, v in irr["scenarios"].items()],
            "expected_irrigation_mm": r["irrigation_attendue_mm"], "expected_volume_m3": r["volume_eau_m3"],
            "irrigation_days_normal_scenario": r["jours_irrigation_scenario_normal"],
            "peak_week_start": r["semaine_la_plus_exigeante"]["debut"],
            "peak_week_expected_mm": r["semaine_la_plus_exigeante"]["irrigation_mm"],
            "weekly_expected_mm": [{"week_start": w.semaine_du, "mm": int(w.attendue)} for w in weeks.itertuples()],
            "season_check": sw,
            "sources": [SRC_IRR], "limitations": [LIM_CAL, "1 mm over 1 ha = 10 m3.", "Scenario draw uses a fixed seed (42)."]}


@mcp.tool()
@audited
def forecast_price_and_harvest(locality: str, crop: str, sowing_date: str,
                               production_t: float = 0.0) -> dict[str, Any]:
    """Harvest date (sowing + cycle), forecast price at harvest (FCFA/kg, with low/high range) and
    whether to sell at harvest or store, with the best month and gain per tonne. If `production_t`
    (tonnes, 0 = unknown) is given, also the split to sell now / store and the total gain. `sowing_date` is YYYY-MM-DD."""
    e = _check_plot(locality, crop, None, sowing_date)
    if e:
        return e
    production_t = production_t or None
    sw = sowing_windows.check(locality, crop, sowing_date)
    if sw["status"] == "out_of_season":
        return err("OUT_OF_SEASON", sw["note"], "Do not draft an advisory; flag the household for the officer.")
    if production_t is not None and not (0.1 <= production_t <= 500):
        return err("PRODUCTION_OUT_OF_RANGE", f"production_t={production_t} outside [0.1, 500].")
    rec = engine.planifier_recolte(locality, crop, sowing_date, production_t)
    w: pd.DataFrame = rec["fenetre"]
    out = {"ok": True, "locality": locality, "crop": crop, "sowing_date": sowing_date,
           "harvest_date": str(rec["date_recolte"]), "harvest_date_fr": _fr(str(rec["date_recolte"])),
           "price_at_harvest_fcfa_kg": int(rec["prix_recolte"]),
           "price_low_fcfa_kg": int(w.bas.iloc[0]), "price_high_fcfa_kg": int(w.haut.iloc[0]),
           "advice": "sell_at_harvest" if rec["conseil"] == "vendre à la récolte" else "store",
           "max_storage_months": engine.STOCKAGE_MAX_MOIS[crop],
           "price_window": [{"month": str(i), "price_fcfa_kg": int(r.prix)} for i, r in w.iterrows()],
           "season_check": sw, "sources": [SRC_PRICE],
           "limitations": ["Range covers about 7 in 10 cases in back-tests on simulated data.",
                           "Storage durations (maize/rice 6 mo, yam 4 mo, cassava/tomato none) are assumptions "
                           "to validate with an agronomist."]}
    if out["advice"] == "store":
        out.update({"best_month": rec["mois_stockage"], "best_price_fcfa_kg": int(rec["prix_stockage"]),
                    "gain_per_tonne_stored_fcfa": int(rec["gain_par_tonne"])})
    if production_t:
        out.update({"production_t": production_t, "sell_at_harvest_t": rec["a_vendre_recolte_t"],
                    "store_t": rec["a_stocker_t"], "total_gain_fcfa": int(rec["gain_total"])})
    return out


# --------------------------------------------------------------------------- action tools (queue only)
def _household_exists(hid: str) -> bool | None:
    if not HOUSEHOLD_DB.exists():
        return None
    c = sqlite3.connect(f"file:{HOUSEHOLD_DB}?mode=ro", uri=True)
    try:
        return c.execute("SELECT 1 FROM households WHERE household_id=?", (hid,)).fetchone() is not None
    finally:
        c.close()


def _load_evidence(ids: list[str]):
    recs, missing = [], []
    for i in ids:
        r = audit.find_evidence(i)
        (recs if r else missing).append(r or i)
    return recs, missing


@mcp.tool()
@audited
def draft_advisory(household_id: str, message_fr: str, evidence_ids: list[str],
                   channel: str = "sms") -> dict[str, Any]:
    """ACTION: put a farmer advisory into the extension officer's approval queue (status
    PENDING_HUMAN_APPROVAL). It is NOT sent. Requirements, enforced here: cite evidence_ids from this
    household's normalize_plot_record plus at least one plan_irrigation / forecast_price_and_harvest /
    predict_season_rainfall call for the same locality, crop and sowing date; message_fr (French, 20-480
    chars) may only contain numbers that appear in the cited evidence. channel: sms | ussd | visit.
    A rejected draft returns the reasons: fix the message and call again."""
    if channel not in ("sms", "ussd", "visit"):
        return err("CHANNEL_INVALID", "channel must be sms, ussd or visit.")
    if not (20 <= len(message_fr) <= 480):
        return err("MESSAGE_LENGTH", f"message is {len(message_fr)} chars; need 20-480.", "Shorten the message.")
    if _household_exists(household_id) is False:
        return err("HOUSEHOLD_NOT_FOUND", f"{household_id} is not in the household register.", HH_HINT)
    if not evidence_ids:
        return err("NO_EVIDENCE", "An advisory without cited data is refused.",
                   "Pass the evidence_id values returned by the compute tools.")
    recs, missing = _load_evidence(evidence_ids)
    if missing:
        return err("EVIDENCE_NOT_FOUND", f"Unknown evidence ids: {missing}.", "Use ids returned by tool calls.")
    norm = [r for r in recs if r["tool"] == "normalize_plot_record" and r["inputs"].get("household_id") == household_id]
    if not norm:
        return err("EVIDENCE_NOT_FOR_HOUSEHOLD",
                   f"Cite a normalize_plot_record evidence called with household_id={household_id}.")
    if norm[0]["output"]["status"] == "UNUSABLE":
        return err("RECORD_UNUSABLE", "The household record is UNUSABLE; use flag_for_officer instead.")
    plot = norm[0]["output"]["plot"]
    calc = [r for r in recs if r["tool"] in ("plan_irrigation", "forecast_price_and_harvest", "predict_season_rainfall")]
    if not calc:
        return err("NO_COMPUTED_EVIDENCE", "Cite at least one plan_irrigation, forecast_price_and_harvest or "
                                           "predict_season_rainfall evidence.")
    for r in calc:
        i = r["inputs"]
        if i.get("locality") != plot["locality"] or (i.get("crop") not in (None, "mais") and i.get("crop") != plot["crop"]) \
                or (i.get("sowing_date") not in (None, plot["sowing_date"])):
            if r["tool"] == "predict_season_rainfall" and i.get("locality") == plot["locality"]:
                continue
            return err("EVIDENCE_MISMATCH", f"Evidence {r['evidence_id']} does not match this household's cleaned "
                                            f"plot {plot}.")
    bad = grounding.ungrounded_numbers(message_fr, [r["output"] for r in recs])
    if bad:
        return err("UNGROUNDED_NUMBERS", f"These numbers are not in the cited evidence: {bad}.",
                   "Rewrite using only figures returned by the tools.")
    issues = norm[0]["output"].get("issues") or []
    note = ("Auto-corrections applied to the register record: " + "; ".join(i["message"] for i in issues)) if issues else None
    row = store.insert("ADVISORY", household_id, channel, message_fr, note, evidence_ids)
    return {"ok": True, "advisory_id": row["id"], "status": row["status"], "duplicate": row["duplicate"],
            "note": "Queued for human approval. Nothing has been sent to the farmer."}


@mcp.tool()
@audited
def flag_for_officer(household_id: str, reason: str, evidence_ids: list[str] = []) -> dict[str, Any]:
    """ACTION: ask the extension officer to look at a household (unusable data, out-of-range values,
    cycle not fully covered, anything the agent is unsure about). Queue-only; no message to the farmer."""
    if not (10 <= len(reason) <= 400):
        return err("REASON_LENGTH", "reason must be 10-400 characters.")
    if _household_exists(household_id) is False:
        return err("HOUSEHOLD_NOT_FOUND", f"{household_id} is not in the household register.", HH_HINT)
    _, missing = _load_evidence(evidence_ids or [])
    if missing:
        return err("EVIDENCE_NOT_FOUND", f"Unknown evidence ids: {missing}.")
    row = store.insert("FLAG", household_id, None, reason, reason, evidence_ids or [])
    return {"ok": True, "flag_id": row["id"], "status": row["status"], "duplicate": row["duplicate"]}


if __name__ == "__main__":
    mcp.run(transport="stdio")
