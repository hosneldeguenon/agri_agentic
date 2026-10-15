# ARCHITECTURE

**Goal.** Cut an extension officer's weekly cross-checking of rain, price and plot data to a review-and-approve step
for a collective of households, without ever letting software talk to a farmer unsupervised.

**Two orchestration modes.** *pipeline* (default, `pipeline.py`): a fixed graph (read row -> normalise -> plan -> forecast -> draft -> retry or flag) where the open-weights LLM is the drafting and recovery node. *agent* (`agent.py`): the LLM chooses every call. We kept pipeline as default because Qwen2.5-7B failed agent mode in all development runs (`LLM_DEV_LOG.md`); the trade-off is less autonomy for reliability, and evaluation reports both. In both modes all tool calls hit the same MCP servers, audit log and server-side checks.

**Agent mode details.** One `smolagents.ToolCallingAgent` per household (fresh context, 12 steps max), run in a plain Python
loop over a collective (`agent.py`). The loop is deliberately code, not a second agent: batch order, crash isolation
and the per-household outcome check (advisory queued / flagged / nothing) must not depend on a 7B model. The agent
follows a seven-step procedure (read row -> normalise -> compute -> draft -> flag) but chooses arguments, reads tool
errors and retries (e.g. after `UNGROUNDED_NUMBERS`). Model: Qwen2.5-7B-Instruct via Ollama (open weights, runs
on national infrastructure, household data stays in-country). A fixed `ScriptedModel` drives the same loop for
GPU-less demos and integration tests.

**MCP servers.**
1. *agri-predict* (built): four read tools (`normalize_plot_record`, `predict_season_rainfall`, `plan_irrigation`,
   `forecast_price_and_harvest`) and two queue-only actions (`draft_advisory`, `flag_for_officer`). Why these
   boundaries: each read tool is a pure function of its arguments with a typed schema and structured errors, so a
   third party can reuse it without our agent; the actions are the only places state changes, and both end in a human
   queue. Cleaning messy records is a tool (deterministic, logged), not an LLM guess.
2. *mcp-server-sqlite* (borrowed): read access to the household register. Reusing it avoids rewriting a SQL server;
   we expose only `read_query`, `list_tables`, `describe_table` to the agent (client-side allow-list), and its calls
   are logged client-side because it has no audit of its own.

**Trust boundary.** The agent can propose; it cannot dispose. Approval, rejection and the (simulated) outbox live in
`review.py`, which is not an MCP tool. Server-side checks in `draft_advisory` (evidence exists, belongs to the household,
matches crop/locality/date, every number grounded) turn "cite your sources" from a prompt wish into a rejection path.

**Audit.** `audit_log.jsonl`: one line per tool call or human decision with UTC time, actor, inputs, outputs and the
previous line's SHA-256. `review.py verify-log` recomputes the chain. Limit: someone with write access to the disk can
rebuild the whole chain; production would anchor hashes externally (signed daily digest on another host).

**Why this shape.** Smaller models fail at arithmetic and at remembering to cite; so numbers come from tools and
citation is enforced by the server. They also fail at format drift; so tool arguments are simple scalars with defaults
removed from the optional path (smolagents marks every parameter required). Costs: ~0 s and 0 tokens for the scripted
policy; LLM token and time cost per household are recorded by `evals/run_evals.py` into `EVALS.md` once run.

**Lessons from real small-model runs** (`LLM_DEV_LOG.md`): reading the register is done by the loop, not the agent; every tool result carries a `next_step`; auto-corrections are recorded by the server on the advisory's officer note rather than asked of the model; compute tools refuse unknown household ids with a format hint.

**Field-driven rule.** `sowing_windows.py` holds sowing windows confirmed by an extension advisor (Zou, maize); `plan_irrigation` and `forecast_price_and_harvest` refuse out-of-window dates (`OUT_OF_SEASON`) and report `season_check: not_covered` elsewhere, so absence of data is visible, not silent.

**Not done.** Real SMS/USSD gateway, per-department sowing windows, pest and credit tools, real data, multi-officer
roles, signed audit anchors.
