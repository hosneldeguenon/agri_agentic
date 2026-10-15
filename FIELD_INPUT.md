# Field input

**Who:** an agricultural extension advisor working in the Zou department, Benin (not named here; no consent to be
named was asked). He follows roughly 100 to 150 producers.
**Format:** one face-to-face conversation, six questions asked by the author.
**Date:** 2026-10-14
**How the answers were recorded (important):** the author took notes during the conversation, then asked an AI
assistant to reword the notes for style and clarity. The wording of the answers below is therefore not verbatim;
the author checked the reworded text against the notes. _Author's declaration:_
[x] I compared the reworded answers with my notes and removed anything the advisor did not say.

## What he said (summary)

1. **Information sources.** Weather from bulletins, other field agents and observation; disease from symptoms,
   experience and exchanges with other technicians; prices from local markets, traders and producers. Main problem:
   information is scattered, hard to verify, or late.
2. **Reach.** About 100 to 150 producers; advice goes out through field visits, group meetings, demonstrations,
   phone calls and WhatsApp. SMS is useful for urgent or simple messages. Visiting everyone regularly is hard.
3. **Would he use a draft-then-validate tool?** Yes, if he keeps control. Each advice should show its sources,
   its update date and the geographic zone. His concerns: AI errors, advice unsuited to local crops, wrong sowing
   dates, outdated weather or market information. Human validation must stay mandatory before any send.
4. **Is a 3-sentence SMS useful?** Yes, if precise and immediately actionable. It should fit the crop, the growth
   stage and local conditions. Missing from my version: urgency level, rain risks, disease signs to watch, nearby
   market prices; for sell-or-store, transport cost, storage options and the producer's cash needs.
5. **Language.** Depends on the zone: Fon is easier in much of the south, Bariba/Dendi/Yom and others in the north;
   French is not always enough. He would want French plus the main local languages of each zone.
6. **Sowing dates (Zou).** Maize: first season mostly March to April depending on when rains actually set in;
   second season mostly August to September. Other crops (cowpea, groundnut, rice, vegetables) differ by variety,
   cycle, water and soil. He never recommends a single date without knowing commune, crop and weather forecast.

## What I changed because of it

- **Sowing-window check** (`agri_predict/sowing_windows.py`, used by `plan_irrigation` and
  `forecast_price_and_harvest`): for **Zou maize only**, a sowing date outside March-April / August-September is
  refused with `OUT_OF_SEASON` and the household is flagged to the officer. This fixed part of the failure we had
  found ourselves (EVALS A18). Tests A21, A22 and the Zou households in the synthetic register exercise it.
- **Zone, crop and computation time on the officer's screen** (`review.py show`), because he asked for sources, date
  and zone on every advice. Sources were already listed.
- Human validation was already mandatory; his answer confirmed that design.

## What I did not change, and why

- **Local languages:** not done (French only). It needs translation and validation by speakers; not feasible in
  the submission window. Next step.
- **Richer SMS content** (urgency, disease signs, market prices, transport and storage costs): data and tools do
  not exist yet. Listed as future work.
- **Other departments and crops:** no confirmed windows, so the season is *not checked* there (EVALS A18 still
  fails for Borgou). One interview covers one zone; more advisors are needed.
- He raised the risk of **wrong sowing dates and AI errors**: this is why every number is checked against the
  cited data and every draft waits for the officer. The checks do not prove the wording is right (README limits).
