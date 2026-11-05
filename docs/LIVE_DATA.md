# Live data: how the dashboard gets fresh results

The deployed dashboard shows results that a **sync** publishes to Upstash Redis. The page reads them with a read-only
token on every load, so a new sync appears without a redeploy. If Redis is not configured it falls back to the
snapshot bundled at build time and labels it "Bundled snapshot, not live".

## What is real, what is synthetic, what is not claimed
| | |
|---|---|
| **Real** | Razorpay **test-mode** payments read through Razorpay's API (real API shapes, fake money); the model's decisions (Amazon Bedrock); the policy, masking and numeric checks (ordinary code); the audit hash chain. |
| **Emulated** | Tenancy. A Razorpay account is one merchant, so payments carry `notes.tenant` (`m_acme` / `m_zen`). Refund requests are recorded in memory only. |
| **Synthetic** | The second suite (`synthetic` source) and any settlements, if the test account returns none. |
| **Not claimed** | Production traffic, real customers, real merchants, any affiliation with Razorpay. Capture is periodic, not a stream. |

## The flow
```
Razorpay TEST API (GET only) -> sanitised snapshot -> scenarios generated from real ids
   -> agent runs, guards on and off, fresh in-memory ledger each time (Nova Lite)
   -> payload (masked) -> Upstash Redis (write token, on the machine running the sync)
   -> /api/data?file=live (login required, read-only token) -> the Live tab
```

## Redis keys (JSON, 120-day expiry, written by `acp/assure/publish.py`)
| Key | Contents |
|---|---|
| `acp:live:latest` | newest sync: model, times, cost, spend-cap flag, per-source summaries with Wilson intervals, provenance lines |
| `acp:live:runs` | newest-first individual runs (capped at 500): scenario, mode, pass/fail, attempted vs effective violations, tool calls, masked answer |
| `acp:live:history` | one line per sync for the trend chart (capped at 200) |
| `acp:live:snapshot` | counts, statuses, tenant split and a hash of the Razorpay snapshot. **No payment contents.** |
| `acp:live:audit` | the sync's audit chain: hashes, head, verified flag |

## One-time setup
1. **Razorpay test keys** (done): put `RAZORPAY_KEY_ID` (starts `rzp_test_`) and `RAZORPAY_KEY_SECRET` in `.env`. The code refuses any other key.
2. **Check the account:** `make live-probe` (read-only; prints counts and field names, never values).
3. **Seed test payments:** an empty account has nothing to read.
   - `make live-seed` is a dry run and shows the 11 orders it would create.
   - `make live-seed-create` creates them in your test account (a write, test mode only, only `POST /orders`).
   - Serve the generated page and pay each order with the UPI id shown (`success@razorpay` or `failure@razorpay`):
     `python -m http.server 8766 -d results/seed`, then open http://localhost:8766/checkout.html. No real money moves.
   - Run `make live-probe` again. It should show at least 8 payments across both tenants.
4. **Upstash Redis:** in Vercel, add the Upstash Redis integration to the project (Storage / Marketplace). It injects connection variables into the project. Check which names it injects: this code accepts `KV_REST_API_URL` or `UPSTASH_REDIS_REST_URL`, and the dashboard reads only `KV_REST_API_READ_ONLY_TOKEN` (or `UPSTASH_REDIS_REST_READ_ONLY_TOKEN`). Free-tier limits come from secondary sources: confirm them on Upstash's pricing page.
5. **Write token for the sync machine:** from the Upstash console copy the REST URL and the normal (write) token into `.env` as `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN`. Never put the write token in Vercel.
6. **Redeploy the dashboard** once so it picks up the new function code and variables (`vercel --prod` from `dashboard/`).

## Running a sync
| Command | Does |
|---|---|
| `make live-probe` | read-only check of the Razorpay account |
| `make live-sync-dry` | whole sync, nothing published; writes `results/live-latest.json` |
| `make live-sync` | the whole sync and publish, then reads the result back to verify |

Options: `--repeats N`, `--model ID`, `--max-cost USD` (default `0.25`; the sync stops cleanly when the estimated
Bedrock cost passes it and says so on the page), `--no-razorpay`, `--no-synthetic`. A small sync costs cents.

## Safety rules and where they are tested
- Test keys only; `rzp_live_` is refused before any request (`tests/test_assure_razorpay.py`, `test_assure_razorpay_seed.py`).
- The reading adapter has no way to write: GET only, no refund or capture call exists in it.
- The seeder can only `POST /orders`, defaults to a dry run, and never touches payments or refunds.
- Only whitelisted fields leave Razorpay; email, phone, VPA, card ids and raw JSON never reach the snapshot, the model, Redis or the audit log.
- Everything is run through the card/ID masker before it is published; raw model text is not published.
- The dashboard function uses only the read-only token, only sends read commands, requires the login first, and never echoes an error detail (`tests/dashboard_live_check.js`).
- `results/live-*`, `results/seed/`, `dashboard/data/live.json` are gitignored.

## When something looks wrong
- **"Bundled snapshot, not live"**: Redis variables are missing on the deployment, or no sync has been published.
- **"Stale: a sync is overdue"**: the last sync is older than twice the expected interval (one week).
- **Probe shows 0 payments**: seed the account (step 3). **Payments but one tenant**: the `notes.tenant` tag was not saved; the adapter also reads the order's notes.
- **Settlements: not available**: Razorpay's docs say test mode has no settlement reports; those scenarios run on the synthetic source.
