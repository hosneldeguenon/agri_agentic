# Development log: first runs with Qwen2.5-7B-Instruct (Ollama, Windows PC, CPU)

Hand-written log of what was observed on one household (HH-BO-001) before the full LLM evaluation. These are
development runs, not part of the scored results in `EVALS.md`. Each run was inspected from `runtime/last_run.json`
and the audit log, and no run produced an advisory.

| Run | What the model did | Time | Cause | Change made |
|---|---|---|---|---|
| 1 | Called `read_query` 12 times in a row, then wrote a generic advisory with no tool data and no sources (correctly NOT queued: no evidence) | 233 s | Stuck on step 1 | Register row is now read by the orchestration code and given to the agent in the task |
| 2 | Called `normalize_plot_record` 12 times in a row. Result was `NEEDS_REVIEW` (typo "Borgu" auto-corrected); the model seems to have treated it as an error to retry | 43 s | Ambiguous status | Every successful tool result now carries a `next_step` field; repeated identical calls are flagged `duplicate_call` |
| 3 | `normalize_plot_record` correct, then `flag_for_officer` with invented `household_id` ("not_needed"), then a hallucinated second household (Zou, cotton, 12000 m²); 10 `HOUSEHOLD_NOT_FOUND` errors | 94 s | Procedure step "also flag NEEDS_REVIEW" was too much for the model; compute tool accepted unknown household ids | Auto-corrections are now written to the advisory's officer note by the server; `normalize_plot_record` refuses unknown household ids with a format hint; task repeats the exact household_id |
| 4 | Called `normalize_plot_record` with all-empty arguments (did not copy the values from the task), invented ids (`hh-000`, `123456`), queried a table that does not exist (`plot_records`); two tool calls were malformed ("no JSON blob"). 5 `HOUSEHOLD_NOT_FOUND`, nothing queued | 67 s | The 7B model cannot reliably choose and parameterise tools in a multi-step procedure | Added **pipeline mode** (default): code chains the tool calls, the LLM only drafts and rewrites the SMS. Agent mode is kept and reported as-is |
| 5 | Pipeline mode, 40 households x 3 repeats: 39/40 each time. HH-CO-008 failed 3/3: the model wrote "1400 mm" where the data said 140 mm (3080 m³ for 2.2 ha), a tenfold error. The server rejected all three drafts (`UNGROUNDED_NUMBERS`) and the household was flagged to the officer instead of queuing a wrong advisory. First drafts also gave a whole-season water total the wrong meaning ("between 31/07 and 21/08") | ~2 s per household | Facts were ambiguously named; the rejection message did not say which value was right | Facts renamed (`season_total_*`, `peak_week_*`) with the meaning spelled out in the prompt; rejection message now lists the only valid values and forbids unit conversion |
| 6 | Same pipeline, 40 households x 3 repeats: 40/40 each time, 34 advisories queued each time, 0 advisories with a number absent from the evidence | ~2 s per household | | none |

Lesson: the guardrails held in every run (nothing unsupported reached the queue). Open-weights 7B models are
unreliable at choosing and parameterising tools but usable as a constrained writer; so the default mode gives the
LLM only the part that needs language, and the evals report both modes separately.

Caveat on run 6: the 40 households are synthetic and were designed by the author, temperature is 0, and the pass rule
checks that an advisory is queued with grounded numbers, not that its wording is good. The French of a 7B model is
still imperfect (e.g. 'Week sommet' for 'semaine de pointe'); the officer edits or rejects before anything is sent.

Note: the water figures quoted in this log (302 mm, 140 mm, 3080 m³, 7550 m³) come from the engine **before** the
ET0 correction (Hargreaves-Samani, 2026-10-14). They are about 40% lower now (for example Borgou maize, 2.5 ha:
122 mm, 3050 m³). The failures described do not depend on those values.
