"""Orchestration: one smolagents ToolCallingAgent run per household, over MCP.

Two MCP servers are connected over stdio:
  * agri-predict (ours)            -> 6 tools (mcp_server.py)
  * mcp-server-sqlite (third party) -> only read_query / list_tables / describe_table are exposed to the
    agent (client-side allow-list). Its write tools are dropped before the agent ever sees them.

Models:
  --mode pipeline (default) | agent   who chooses the tool calls: the code (LLM drafts and recovers) or the LLM.
  --model ollama    open-weights model served by Ollama (default qwen2.5:7b-instruct). THE REAL AGENT.
  --model scripted  a fixed, non-LLM policy that drives the same MCP tools through the same smolagents
                    loop. It exists so a stranger can run the full pipeline without a GPU and so the
                    plumbing + guardrails can be tested. It says nothing about LLM quality.
"""
import argparse
import ast
import json
import os
import re
import sqlite3
import sys
import time
import uuid

from mcp import StdioServerParameters
from smolagents import MCPClient, ToolCallingAgent
from smolagents.models import ChatMessage, ChatMessageToolCall, ChatMessageToolCallFunction, Model

from . import audit, store
from .config import HOUSEHOLD_DB, ROOT, runtime_dir

SQLITE_ALLOWED = {"read_query", "list_tables", "describe_table"}

INSTRUCTIONS = """You are the planning assistant of an agricultural extension office in Benin.
For ONE household you prepare a DRAFT advisory for the extension officer. You never contact the farmer:
there is no tool to send, approve, order inputs or raise credit alerts, and you must not try.

Procedure:
1. read_query: SELECT * FROM households WHERE household_id = '<ID>'  (SELECT only).
2. normalize_plot_record with the raw fields exactly as stored (keep empty strings empty) and household_id.
   Never correct raw values yourself.
3. status UNUSABLE -> flag_for_officer (reason in plain English, evidence_ids=[normalize evidence_id]) and finish.
   Never guess a missing or invalid value.
4. Otherwise call plan_irrigation and forecast_price_and_harvest with the CLEANED plot values from step 2
   (production_t = expected_production_t with decimal point, or 0 if empty; always pass every argument).
5. Write message_fr: simple French, SMS style, max 450 characters, mention the water need, the harvest date
   and the sell/store advice, say if cycle_fully_covered is false, write units as m³ (not m3), and end by
   saying these are estimates to confirm with the advisor. Copy numbers from tool outputs ONLY.
6. draft_advisory (channel "sms") with evidence_ids = [normalize, plan_irrigation, forecast_price_and_harvest]. If it is
   rejected, read error_code/hint, fix and retry (at most 2 retries). If it still fails, flag_for_officer.
7. final_answer: one short line saying what you queued (advisory id / flag id).
The server records any auto-corrections for the officer by itself: do not flag them.
Use the household_id given in the task exactly, in every tool call; never invent one.
Call each tool once per household unless an error_code asks you to retry. Every successful result has a
`next_step` field: follow it. NEEDS_REVIEW is not an error."""


INSTRUCTIONS_PREFETCH = INSTRUCTIONS.replace(
    "1. read_query: SELECT * FROM households WHERE household_id = '<ID>'  (SELECT only).",
    "1. The household's register row is already given in the task. Do NOT call read_query.")


# ----------------------------------------------------------------------------- MCP wiring
def server_params() -> list[StdioServerParameters]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    ours = StdioServerParameters(command=sys.executable, args=["-m", "agri_predict.mcp_server"], env=env, cwd=str(ROOT))
    sqlite = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from mcp_server_sqlite import main; main()", "--db-path", str(HOUSEHOLD_DB)], env=env)
    return [ours, sqlite]


def _log_third_party(tool):
    """Third-party server doesn't write to our audit log: log its calls client-side."""
    orig = tool.forward

    def forward(*a, **kw):
        out = orig(*a, **kw)
        audit.append("tool_call", "agent", f"sqlite.{tool.name}", kw or {"args": list(a)},
                     {"result": str(out)[:2000]})
        return out
    tool.forward = forward
    return tool


def filter_tools(tools):
    keep = []
    for t in tools:
        if t.name in {"append_insight", "write_query", "create_table"}:
            continue  # never exposed to the agent
        keep.append(_log_third_party(t) if t.name in SQLITE_ALLOWED else t)
    return keep


# ----------------------------------------------------------------------------- scripted (non-LLM) policy
def _msg(name, args):
    return ChatMessage(role="assistant", content="", tool_calls=[ChatMessageToolCall(
        id=f"call_{uuid.uuid4().hex[:8]}", type="function",
        function=ChatMessageToolCallFunction(name=name, arguments=args))])


def _last_observation(messages) -> str:
    for m in reversed(messages):
        role = m.get("role") if isinstance(m, dict) else m.role
        if str(getattr(role, "value", role)) == "tool-response":
            c = m["content"] if isinstance(m, dict) else m.content
            txt = c if isinstance(c, str) else " ".join(p.get("text", "") for p in c)
            return txt.split("Observation:", 1)[-1].strip()
    return ""


def _json(txt):
    try:
        return json.loads(txt[txt.index("{"):])
    except Exception:
        return {}


class ScriptedModel(Model):
    """Fixed procedure mirroring INSTRUCTIONS. Not an LLM."""

    def __init__(self, drop_digits_once: bool = False):
        super().__init__(model_id="scripted-policy")
        self.reset()
        self.sabotage = drop_digits_once  # test hook: first draft contains an invented number

    def reset(self):
        self.i, self.hid, self.row, self.norm, self.plan, self.fc = 0, None, {}, {}, {}, {}
        self.retries, self.queued = 0, False

    def _draft_text(self, invent=False):
        p, plan, fc = self.norm["plot"], self.plan, self.fc
        crop = {"mais": "maïs", "riz": "riz", "igname": "igname", "manioc": "manioc", "tomate": "tomate"}[p["crop"]]
        y, m, d = p["sowing_date"].split("-")
        t = (f"Parcelle de {crop} ({p['area_ha']} ha, semis le {d}/{m}/{y}) : prévoyez environ "
             f"{plan['expected_irrigation_mm']} mm d'eau en plus de la pluie ({plan['expected_volume_m3']} m³), "
             f"surtout la semaine du {plan['peak_week_start']}. ")
        if not plan["cycle_fully_covered"]:
            t += f"Le plan couvre {plan['planned_days']} jours sur {plan['cycle_days']}. "
        t += f"Récolte vers le {fc['harvest_date_fr']}, prix prévu {fc['price_at_harvest_fcfa_kg']} FCFA/kg"
        if fc["advice"] == "store":
            t += f" ; mieux vaut stocker jusqu'en {fc['best_month'][5:7]}/{fc['best_month'][:4]}"
        else:
            t += " ; mieux vaut vendre à la récolte"
        t += ". Estimations à confirmer avec votre conseiller."
        return t + (" Gain de 99999 FCFA." if invent else "")

    def generate(self, messages, stop_sequences=None, response_format=None, tools_to_call_from=None, **kw):
        obs = _last_observation(messages)
        i, self.i = self.i, self.i + 1
        if i == 0:
            first = str(messages[1] if len(messages) > 1 else messages[0])
            self.hid = re.search(r"HH-[A-Z]{2}-\d{3}", " ".join(str(m) for m in messages)).group(0)
            return _msg("read_query", {"query": f"SELECT * FROM households WHERE household_id = '{self.hid}'"})
        if i == 1:
            rows = ast.literal_eval(obs)
            if not rows:
                return _msg("final_answer", {"answer": f"{self.hid}: not in register"})
            r = rows[0]
            self.row = r
            nz = lambda v: "" if v in ("", None) else str(v)
            return _msg("normalize_plot_record", {
                "locality_raw": nz(r["locality_raw"]), "crop_raw": nz(r["crop_raw"]), "area_raw": nz(r["area_raw"]),
                "area_unit": nz(r["area_unit"]), "sowing_date_raw": nz(r["sowing_date_raw"]),
                "household_id": self.hid})
        if i == 2:
            self.norm = _json(obs)
            if self.norm.get("status") == "UNUSABLE":
                why = "; ".join(x["message"] for x in self.norm["issues"] if x["severity"] == "error")
                return _msg("flag_for_officer", {"household_id": self.hid, "reason": ("Unusable record: " + why)[:400],
                                                 "evidence_ids": [self.norm["evidence_id"]]})
            p = self.norm["plot"]
            return _msg("plan_irrigation", {"locality": p["locality"], "crop": p["crop"],
                                            "area_ha": p["area_ha"], "sowing_date": p["sowing_date"]})
        if self.norm.get("status") == "UNUSABLE" or (i >= 4 and not self.plan.get("ok")):
            return _msg("final_answer", {"answer": f"{self.hid}: flagged"})
        if i == 3:
            self.plan = _json(obs)
            if not self.plan.get("ok"):
                return _msg("flag_for_officer", {"household_id": self.hid, "evidence_ids": [self.norm["evidence_id"]],
                                                 "reason": f"Compute failed: {self.plan.get('error_code')}"[:400]})
            p = self.norm["plot"]
            prod = str(self.row.get("expected_production_t") or "").replace(",", ".")
            args = {"locality": p["locality"], "crop": p["crop"], "sowing_date": p["sowing_date"],
                    "production_t": float(prod) if prod else 0.0}
            return _msg("forecast_price_and_harvest", args)
        if i == 4:
            self.fc = _json(obs)
            ev = [self.norm["evidence_id"], self.plan["evidence_id"], self.fc["evidence_id"]]
            return _msg("draft_advisory", {"household_id": self.hid, "message_fr": self._draft_text(self.sabotage),
                                           "evidence_ids": ev, "channel": "sms"})
        if i == 5:
            res = _json(obs)
            ev = [self.norm["evidence_id"], self.plan["evidence_id"], self.fc["evidence_id"]]
            if not res.get("ok") and self.retries < 2:
                self.retries += 1
                self.i = 5
                self.sabotage = False
                return _msg("draft_advisory", {"household_id": self.hid, "message_fr": self._draft_text(False),
                                               "evidence_ids": ev, "channel": "sms"})
            if not res.get("ok"):
                return _msg("flag_for_officer", {"household_id": self.hid, "evidence_ids": [self.norm["evidence_id"]],
                                                 "reason": f"Draft rejected: {res.get('error_code')}"})
            self.queued = True
            return _msg("final_answer", {"answer": f"{self.hid}: advisory queued ({res.get('advisory_id')})"})
        return _msg("final_answer", {"answer": f"{self.hid}: done (advisory queued={self.queued})"})


# ----------------------------------------------------------------------------- run
def build_model(kind: str):
    if kind == "scripted":
        return ScriptedModel()
    from smolagents import OpenAIServerModel
    return OpenAIServerModel(model_id=os.getenv("AGRI_MODEL", "qwen2.5:7b-instruct"),
                             api_base=os.getenv("OLLAMA_URL", "http://localhost:11434/v1"),
                             api_key="ollama", temperature=0.0)


def household_ids(group=None):
    c = sqlite3.connect(f"file:{HOUSEHOLD_DB}?mode=ro", uri=True)
    q, a = "SELECT household_id FROM households", ()
    if group:
        q, a = q + " WHERE group_id=?", (group,)
    ids = [r[0] for r in c.execute(q + " ORDER BY household_id", a)]
    c.close()
    return ids


def prefetch_row(tools, hid: str):
    """Read the register row through the third-party MCP tool, in code. A first real run showed Qwen2.5-7B
    looping on read_query for all 12 steps; reading is not a decision, so the loop does it and hands the
    row to the agent, which still plans and executes every compute/draft/flag step itself."""
    tool = next(t for t in tools if t.name == "read_query")
    rows = ast.literal_eval(str(tool(query=f"SELECT * FROM households WHERE household_id = '{hid}'")))
    return rows[0] if rows else None


def run_household(tools, model, hid: str, max_steps: int = 12, prefetch=None) -> dict:
    if hasattr(model, "reset"):
        model.reset()
    if prefetch is None:
        prefetch = not isinstance(model, ScriptedModel)
    task = f"Prepare the draft advisory for household {hid} (household_id = '{hid}': use this exact value in every tool call)."
    if prefetch:
        row = prefetch_row(tools, hid)
        if row is None:
            return {"household_id": hid, "answer": "not in register", "crashed": None, "seconds": 0.0,
                    "tool_sequence": [], "errors": [], "advisory_queued": False, "flagged": False,
                    "input_tokens": 0, "output_tokens": 0, "steps": 0}
        task += f" Register row (already read for you): {json.dumps(row, ensure_ascii=False)}"
    agent = ToolCallingAgent(tools=tools, model=model, max_steps=max_steps,
                             instructions=INSTRUCTIONS_PREFETCH if prefetch else INSTRUCTIONS,
                             add_base_tools=False, verbosity_level=0)
    n0 = len(audit.read_all())
    t0 = time.time()
    try:
        answer, crashed = str(agent.run(task)), None
    except Exception as e:  # model/network failure must not kill the batch
        answer, crashed = "", f"{type(e).__name__}: {e}"
    new = audit.read_all()[n0:]
    calls = [r for r in new if r["event"] == "tool_call"]
    usage = getattr(agent.monitor, "total_input_token_count", 0), getattr(agent.monitor, "total_output_token_count", 0)
    queued = [r["output"] for r in calls if r["tool"] in ("draft_advisory", "flag_for_officer") and r["output"].get("ok")]
    return {"household_id": hid, "answer": answer, "crashed": crashed, "seconds": round(time.time() - t0, 2),
            "tool_sequence": [r["tool"] for r in calls],
            "errors": [r["output"].get("error_code") for r in calls if r["output"].get("ok") is False],
            "advisory_queued": any("advisory_id" in q for q in queued), "flagged": any("flag_id" in q for q in queued),
            "input_tokens": usage[0], "output_tokens": usage[1], "steps": len(agent.memory.steps) - 1}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", choices=["ollama", "scripted"], default="ollama")
    ap.add_argument("--mode", choices=["pipeline", "agent"], default="pipeline",
                    help="pipeline: code-orchestrated graph, LLM drafts + recovers (default, works with 7B models). "
                         "agent: LLM chooses every tool call (failed with Qwen2.5-7B, see LLM_DEV_LOG.md)")
    ap.add_argument("--group", help="collective id, e.g. GV-BOR (default: all households)")
    ap.add_argument("--household", help="a single household id")
    a = ap.parse_args(argv)
    ids = [a.household] if a.household else household_ids(a.group)
    if a.mode == "pipeline":
        from . import pipeline
        model = pipeline.build_writer(a.model)
        runner = pipeline.run_pipeline_household
    else:
        model = build_model(a.model)
        runner = run_household
    results = []
    with MCPClient(server_params(), structured_output=False) as all_tools:
        tools = filter_tools(all_tools)
        for hid in ids:
            r = runner(tools, model, hid)
            results.append(r)
            outcome = "ADVISORY" if r["advisory_queued"] else "FLAG" if r["flagged"] else "NOTHING"
            print(f"{hid:<11} {outcome:<9} steps={r['steps']:<2} {r['seconds']:>5}s  errors={r['errors']}  "
                  f"{'CRASH ' + r['crashed'] if r['crashed'] else ''}")
    (runtime_dir() / "last_run.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    ok, n, msg = audit.verify()
    pend = store.list_rows(store.PENDING)
    print(f"\n{len(pend)} items waiting for the officer (python -m agri_predict.review list). audit log: {n} entries, {msg}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
