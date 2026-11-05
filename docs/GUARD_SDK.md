# Using the guard on your own agent

`acp.guard` puts the same kinds of controls the benchmark measures in front of **your** agent's tools, with the rules in a
YAML file instead of code. Every tool call goes through it: it checks the call, runs your real function only if the call is
allowed, masks what comes back, holds writes that need a person's sign-off, and records each decision in the hash-chained
audit log.

It is a library, not a service. It is not published to a package index; install it from a checkout:

```bash
pip install ".[guard]"        # PyYAML and pydantic; Python 3.11+
python examples/guard_quickstart.py
```

## Thirty seconds

```python
from acp.guard import Guard

guard = Guard.from_file("guard.yaml", audit_db="audit.db")      # the rules, and where the log goes
guard.register("get_payment", get_payment)                       # your real functions, named as in the rules
guard.register("propose_refund", propose_refund)

session = guard.session(tenant="m_acme", user_text="Refund pay_A001 for Rs 500")   # one task for one customer
result = session.call("get_payment", payment_id="pay_A001")      # a ToolOutcome
tool_result_for_the_model = result.for_model()                   # the data, or a refusal that reveals nothing more
shown = session.check_answer(model_answer)                       # mask card data, withhold unverifiable amounts
```

`session.tool("get_payment")` returns a plain function (keyword arguments in, a model-safe dict out) for agent frameworks that
want a callable per tool.

## The rules file

See [examples/payments.guard.yaml](../examples/payments.guard.yaml). Unknown keys are an error, so a typo is caught when the file loads, not
silently ignored.

| Rule | Where | What it does |
|---|---|---|
| `budget.max_calls` | top level | At most this many tool calls per task. |
| `masking` | top level | Mask card, Aadhaar, PAN, UPI id, phone and email in results, answers and the log. |
| `numeric_fidelity` | top level | An answer may only state rupee amounts that a tool returned (as `*_paise`) or the user typed; otherwise it is withheld. |
| `scope.owner_field` | read tool | The result names its owner in this field; another tenant's object is reported as not found and no data is returned. With `list_key`, foreign rows are dropped from the list. A result with no owner field is treated as foreign (fails closed). |
| `max_per_session` | write tool | At most N requests of this tool per task. |
| `require_named` | write tool | An argument must come from the user's own message: `id` (the value appears verbatim) or `amount_paise` (the value is an amount the message states). Text inside a tool result can never authorise a write. |
| `limits` | write tool | Numeric `min` / `max` on an argument. |
| `approval` | write tool | The call is **not run**. It becomes an approval request that a person decides. `tiers` mark requests above a threshold as `senior`, which need a written reason. |

Rules that cannot be written in a file (they need a lookup in your system) go in code, next to the registration:

```python
guard.add_check("propose_refund", lambda args, session: None if ok(args, session) else ("over_limit", "More than is refundable."))
```

A check returns `None` to allow or `(code, reason)` to refuse. The quickstart uses two: the payment must belong to the tenant, and the
amount must fit what is left refundable.

## Approvals

```python
guard = Guard.from_file("guard.yaml", on_approval=notify_someone)
outcome = session.call("propose_refund", payment_id="pay_A001", amount_paise=50_000)
outcome.code            # "pending_approval"; your function has NOT run
guard.pending()         # [ApprovalRequest, ...]
guard.decide(outcome.approval.id, True, by="Priya", note="duplicate order")   # now it runs, once
```

A request can be decided once. Asking twice for the same thing returns the same request. `by` is required, and every decision (and
the execution) is written to the audit log with that name.

## Block alerts

`Guard(..., on_block=callback)` calls your function with the `ToolOutcome` whenever a call is refused. This repository's worker uses
the same idea to post to Slack (see [HOSTED_WORKER.md](HOSTED_WORKER.md)).

## The audit log and the report

Pass `audit_db="audit.db"` (or an `AuditLog`) and every run start, tool call, approval request, decision, execution and answer is
appended to a SHA-256 hash chain. Export it for a compliance reader:

```bash
python -m acp.assure.report --db audit.db --format html --out report.html     # print to PDF from a browser
python -m acp.assure.report --db audit.db --format json --out report.json
python scripts/verify_audit_export.py report.json                              # standard library only
```

## What this is not (read before relying on it)

- **The benchmark numbers do not transfer.** The measured results on the dashboard (0 of 93 critical outcomes guarded, 36 of 93
  unguarded) come from the built-in payments runtime in `acp/assure/tools.py` and `policy.py`. `acp.guard` is a separate,
  generic engine with the same kinds of rules. Its tests show each rule works and that the quickstart's attacks are stopped; they do
  not measure how a model behaves behind it. Run your own scenarios.
- **`require_named` is a heuristic** on the user's wording. "Refund in full" is not understood by the file (an amount the user did not
  type is refused), so a "full refund" flow needs a custom check.
- **Numeric fidelity covers rupee amounts held as integer paise in fields ending `_paise`**, nothing else.
- **Masking is validated pattern matching**, not a data-loss-prevention product.
- **The audit log is tamper-evident, not tamper-proof.** Record its head hash somewhere the database administrator cannot write.
- **Approvals held in the library live in memory** of the process. The dashboard's approval queue is a separate implementation that the
  worker fills from the benchmarked runtime; wiring `acp.guard` approvals into that queue is not done.
- No authentication or authorisation of *who* may decide: your code decides who may call `guard.decide`.
