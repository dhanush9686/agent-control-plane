# Known issues

## 1. [FIXED 2026-11-05] A single question could take minutes when the provider asked us to wait

**Status: fixed.** `call_with_retries` (acp/llm.py) now caps every sleep at 10 s, enforces a per-call deadline (60 s), and
abandons a provider at once when it asks for more than 15 s (the daily-cap case), so the fallback model takes over.
Covered by `tests/test_bedrock.py` (`test_a_huge_retry_after_makes_us_give_up_immediately_instead_of_sleeping`). The
service now runs on Amazon Bedrock, which has not shown this behaviour (median ask 2.7 s in the live run). Original report:
- **Seen:** in a live run on a 410-page document, one `/ask` took 93 s. The trace showed the judge call at 0.9 s and the
  `llm.synthesize` call to `openai/gpt-oss-120b` at 90 s with 1 retry. All other questions took 2-14 s.
- **Cause (proven 2026-11-05):** `openai/gpt-oss-120b` had used its free-tier daily token cap (Groq: 200,000 tokens/day;
  the error said "Used 199333, Requested 2626"). Groq answers such a request with HTTP 429 and `retry-after: 847`
  (14 minutes). `GroqLLM._call` (`acp/llm.py`, the `time.sleep(delay)` in the retry loop) obediently sleeps that long
  inside the web request, so the request looks hung and the fallback models are never tried. A stack dump
  (`kill -USR1 <server pid>`) showed the thread parked in that sleep. Tiny requests still succeed under the cap,
  which is why quick health probes looked fine while real questions stalled.
- **Why our own budget did not prevent it:** `ModelBudget` counts only tokens spent through this server (180,000/day
  default). The Groq cap is per organisation, so evals and other tools that share the key also spend it.
- **Planned fix:** if the provider asks us to wait longer than a few seconds, abandon that model at once and use the
  next one in the fallback chain (or return HTTP 429 with the real wait), instead of holding the request open. Add a
  per-request deadline, and learn the real remaining quota from Groq's response headers. Test with a fake provider
  that returns `Retry-After: 847`.
- **Impact:** no wrong answers. One earlier slow answer (93 s) was correct and ignored an injected instruction; the
  worst case is a request that blocks for the full Retry-After (up to the rest of the day for a daily cap).

## 2. [MITIGATED 2026-11-05] The cheapest model (Nova Micro) was a weak judge on long contexts
- **Seen (live, 410-page document):** after a first search missed, the agent rewrote the query and the judge then saw 10 chunks,
  with the right one (page 12) ranked first. Nova Micro answered `sufficient=false` and the agent refused, even for the
  plain question. Same judge step on the same 10 chunks: Nova Lite and Nova Pro both said `sufficient=true`. Micro also
  answered "game" to "reply with exactly one word: pong".
- **A/B on the real document (14 questions per setup; see DECISIONS #21):** Micro everywhere 13/14 (one injected question
  refused); Lite everywhere 14/14, same on clean/paraphrased/unanswerable questions, no injected text leaked in any setup.
- **Change made:** default is now Nova Lite for judging, refining and answering, Micro as the fallback, Pro as the eval
  grader. Cost is about $0.26 per 1,000 questions.
- **Still true:** the sample is small (14 questions, one document), so the 13/14 vs 14/14 gap is not statistically
  meaningful by itself. When the question carries injected instructions *and* the context is large, every Nova model
  we tried (Micro, Lite, Pro) judged the context insufficient in a direct test, which is a safe but unhelpful failure.
- **Ideas:** show the judge fewer, better-ranked chunks after a retry (re-rank instead of appending), let the answer
  step see the top chunks even when the judge is unsure.
