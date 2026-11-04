# Known issues (open)

## 1. A single question can take ~90 s when Groq is slow or rate-limiting (found 2026-11-05)
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
