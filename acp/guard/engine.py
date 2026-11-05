"""The guard engine: put the rules from a config file in front of any agent's tools.

Analogy: a checkpoint between the agent and your systems. Every tool call goes through it. It checks the call against
the rules, runs the real function only if the call is allowed, masks what comes back, and writes each decision into a
tamper-evident log. A write that needs a person's sign-off is held as a request until someone decides it.

Honest limits (the same as the benchmarked engine): `require_named` is a heuristic on the user's wording, masking is
validated pattern matching and not a data-loss-prevention product, numeric fidelity covers currency amounts held as
integer paise only, and the audit log is tamper-evident, not tamper-proof.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from acp.assure.audit import AuditLog, redact_deep
from acp.assure.numeric import amounts_in, unsupported_amounts
from acp.assure.pii import find_pii, redact
from acp.guard.config import GuardConfig, ToolRule, load_config

Check = Callable[[dict[str, Any], "Session"], "tuple[str, str] | None"]
WITHHELD = "I can't show this answer because it contains amounts I could not verify ({amounts})."


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class ApprovalRequest:
    """A write the rules say a person must approve. `args` are masked; the raw arguments stay inside the guard."""

    id: str
    tool: str
    args: dict[str, Any]
    tenant: str
    tier: str
    run_id: str | None
    created_at: str
    state: Literal["pending", "approved", "rejected"] = "pending"
    decided_by: str | None = None
    decided_at: str | None = None
    note: str = ""
    result: Any = None
    error: str = ""
    _raw_args: dict[str, Any] = field(default_factory=dict, repr=False)

    def public(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}


@dataclass
class ToolOutcome:
    tool: str
    args: dict[str, Any]
    allowed: bool
    code: str  # ok | pending_approval | not_found | not_named_by_user | amount_not_requested | over_limit | ...
    reason: str = ""
    result: Any = None
    effect: Literal["none", "pending", "executed"] = "none"
    approval: ApprovalRequest | None = None
    ms: float = 0.0

    def for_model(self) -> dict[str, Any]:
        """What to hand back to the model as the tool result: the data, or a refusal that reveals nothing more."""
        if not self.allowed:
            return {"error": self.reason, "code": self.code}
        if isinstance(self.result, dict):
            return self.result
        return {"result": self.result}


@dataclass
class AnswerCheck:
    text: str  # what to show the user
    masked: list[str]  # kinds of data that were masked
    unsupported: list[str]  # amounts that matched no tool result
    withheld: bool


class Guard:
    def __init__(
        self,
        config: GuardConfig,
        *,
        audit: AuditLog | None = None,
        on_approval: Callable[[ApprovalRequest], None] | None = None,
        on_block: Callable[[ToolOutcome], None] | None = None,
    ) -> None:
        self.config, self.audit = config, audit
        self.on_approval, self.on_block = on_approval, on_block
        self._fns: dict[str, Callable[..., Any]] = {}
        self._checks: dict[str, list[Check]] = {}
        self._approvals: dict[str, ApprovalRequest] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_file(cls, path: str | Path, *, audit_db: str | Path | None = None, **kw: Any) -> Guard:
        audit = None
        if audit_db is not None:
            from acp import store

            audit = AuditLog(store.connect(audit_db))
        return cls(load_config(path), audit=audit, **kw)

    # ----- wiring
    def register(self, name: str, fn: Callable[..., Any]) -> Guard:
        """Tell the guard which real function sits behind a tool name in the rules file."""
        if name not in self.config.tools:
            raise KeyError(f"{name!r} is not in the rules file, so it can never be called through the guard")
        self._fns[name] = fn
        return self

    def add_check(self, tool: str, fn: Check) -> Guard:
        """A rule that does not fit a config file, in code: return None to allow, or (code, reason) to refuse."""
        if tool not in self.config.tools:
            raise KeyError(f"{tool!r} is not in the rules file")
        self._checks.setdefault(tool, []).append(fn)
        return self

    def session(self, tenant: str, user_text: str, run_id: str | None = None) -> Session:
        return Session(self, tenant, user_text, run_id)

    # ----- approvals
    def pending(self) -> list[ApprovalRequest]:
        return [a for a in self._approvals.values() if a.state == "pending"]

    def approval(self, approval_id: str) -> ApprovalRequest | None:
        return self._approvals.get(approval_id)

    def decide(self, approval_id: str, approve: bool, by: str, note: str = "") -> ApprovalRequest:
        """Record a person's decision. Approving runs the held function once; a request can be decided only once."""
        if not by.strip():
            raise ValueError("A decision needs the name of the person making it.")
        with self._lock:
            req = self._approvals.get(approval_id)
            if req is None:
                raise KeyError(f"No such approval: {approval_id}")
            if req.state != "pending":
                raise ValueError(f"Approval {approval_id} was already {req.state}.")
            if req.tier != "standard" and approve and len(note.strip()) < 5:
                raise ValueError(f"A {req.tier}-tier approval needs a written reason.")
            req.state = "approved" if approve else "rejected"
            req.decided_by, req.decided_at, req.note = by.strip(), _now(), note.strip()[:300]
        self._log(
            by, "approval_decided",
            {"approval_id": req.id, "tool": req.tool, "tier": req.tier, "decision": req.state, "note": req.note},
            req.run_id,
        )  # fmt: skip
        if approve:
            self._execute(req)
        return req

    def _execute(self, req: ApprovalRequest) -> None:
        fn = self._fns.get(req.tool)
        try:
            if fn is None:
                raise LookupError("no function registered")
            out = fn(**req._raw_args)
            req.result = redact_deep(out) if self.config.masking else out
            self._log(
                req.decided_by or "",
                "approval_executed",
                {"approval_id": req.id, "tool": req.tool, "ok": True},
                req.run_id,
            )
        except Exception as e:  # noqa: BLE001 - the decision stands; the failure is recorded, not hidden
            req.error = type(e).__name__
            failed = {"approval_id": req.id, "tool": req.tool, "ok": False, "error": req.error}
            self._log(req.decided_by or "", "approval_executed", failed, req.run_id)

    def _log(self, actor: str, action: str, detail: dict[str, Any], run_id: str | None) -> None:
        if self.audit:
            self.audit.append(actor, action, detail, run_id)


class Session:
    """One task for one tenant: its own call budget, its own record of what the user actually asked for."""

    def __init__(self, guard: Guard, tenant: str, user_text: str, run_id: str | None) -> None:
        self.guard, self.tenant, self.user_text = guard, tenant, user_text
        self.run_id = run_id or hashlib.sha256(f"{tenant}|{user_text}|{time.time()}".encode()).hexdigest()[:12]
        self.calls = 0
        self.writes: dict[str, int] = {}
        self.outcomes: list[ToolOutcome] = []
        masked_prompt = redact(user_text)[0] if guard.config.masking else user_text
        guard._log(tenant, "run_start", {"prompt": masked_prompt}, self.run_id)

    # ----- calling tools
    def tool(self, name: str) -> Callable[..., dict[str, Any]]:
        """A plain function for an agent framework: call it with keyword arguments, get a model-safe dict back."""
        return lambda **kw: self.call(name, **kw).for_model()

    def call(self, name: str, **kwargs: Any) -> ToolOutcome:
        t0 = time.perf_counter()
        out = self._decide(name, kwargs)
        out.ms = round((time.perf_counter() - t0) * 1000, 3)
        self.outcomes.append(out)
        self.guard._log(
            self.tenant, "tool_call",
            {"tool": name, "args": kwargs, "allowed": out.allowed, "code": out.code, "effect": out.effect}, self.run_id,
        )  # fmt: skip
        if not out.allowed and self.guard.on_block:
            self.guard.on_block(out)
        return out

    def _deny(self, name: str, args: dict[str, Any], code: str, reason: str) -> ToolOutcome:
        safe = redact_deep(args) if self.guard.config.masking else args
        return ToolOutcome(name, safe, False, code, reason)

    def _decide(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        cfg = self.guard.config
        rule = cfg.tools.get(name)
        fn = self.guard._fns.get(name)
        if rule is None or fn is None:
            return self._deny(name, args, "unknown_tool", f"There is no tool named {name}.")
        self.calls += 1
        if self.calls > cfg.max_calls:
            return self._deny(name, args, "call_budget", f"Tool-call budget of {cfg.max_calls} per task is used up.")
        if rule.effect == "write":
            if (bad := self._check_write(name, rule, args)) is not None:
                return bad
            for chk in self.guard._checks.get(name, []):
                if (verdict := chk(args, self)) is not None:
                    return self._deny(name, args, verdict[0], verdict[1])
            if rule.approval and rule.approval.required:
                return self._hold(name, rule, args)
            return self._run(name, rule, fn, args)
        for chk in self.guard._checks.get(name, []):
            if (verdict := chk(args, self)) is not None:
                return self._deny(name, args, verdict[0], verdict[1])
        return self._run(name, rule, fn, args)

    def _check_write(self, name: str, rule: ToolRule, args: dict[str, Any]) -> ToolOutcome | None:
        if rule.max_per_session and self.writes.get(name, 0) >= rule.max_per_session:
            return self._deny(name, args, "session_limit", f"At most {rule.max_per_session} {name} requests per task.")
        wanted = {int(a * 100) for a in amounts_in(self.user_text)}
        for arg, kind in rule.require_named.items():
            if arg not in args:
                return self._deny(name, args, "bad_arguments", f"Missing argument: {arg}.")
            if kind == "id" and str(args[arg]) not in self.user_text:
                return self._deny(name, args, "not_named_by_user", f"{arg} must be one the requester named.")
            if kind == "amount_paise":
                try:
                    ok = int(args[arg]) in wanted
                except (TypeError, ValueError):
                    ok = False
                if not ok:
                    return self._deny(
                        name, args, "amount_not_requested", f"{arg} was not an amount the requester asked for."
                    )
        for arg, lim in rule.limits.items():
            if arg not in args:
                return self._deny(name, args, "bad_arguments", f"Missing argument: {arg}.")
            try:
                v = float(args[arg])
            except (TypeError, ValueError):
                return self._deny(name, args, "bad_arguments", f"{arg} must be a number.")
            if (lim.min is not None and v < lim.min) or (lim.max is not None and v > lim.max):
                return self._deny(name, args, "over_limit", f"{arg} is outside the allowed range.")
        return None

    def _tier(self, rule: ToolRule, args: dict[str, Any]) -> str:
        best, best_above = "standard", float("-inf")
        for t in rule.approval.tiers if rule.approval else []:
            try:
                v = float(args.get(t.field))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if v > t.above and t.above > best_above:
                best, best_above = t.tier, t.above
        return best

    def _hold(self, name: str, rule: ToolRule, args: dict[str, Any]) -> ToolOutcome:
        cfg = self.guard.config
        key = hashlib.sha256(f"{self.tenant}|{name}|{sorted(args.items(), key=str)}".encode()).hexdigest()[:16]
        aid = "apr_" + key
        existing = self.guard._approvals.get(aid)
        if existing is None:  # asking twice for the same thing returns the same request, not a second one
            safe = redact_deep(args) if cfg.masking else dict(args)
            existing = ApprovalRequest(
                aid, name, safe, self.tenant, self._tier(rule, args), self.run_id, _now(), _raw_args=dict(args)
            )
            self.guard._approvals[aid] = existing
            self.writes[name] = self.writes.get(name, 0) + 1
            self.guard._log(
                self.tenant,
                "approval_requested",
                {"approval_id": aid, "tool": name, "tier": existing.tier, "args": args},
                self.run_id,
            )  # fmt: skip
            if self.guard.on_approval:
                self.guard.on_approval(existing)
        result = {"status": "pending_approval", "approval_id": aid, "tier": existing.tier, "executes": False}
        return ToolOutcome(name, existing.args, True, "pending_approval", "", result, "pending", existing)

    def _run(self, name: str, rule: ToolRule, fn: Callable[..., Any], args: dict[str, Any]) -> ToolOutcome:
        cfg = self.guard.config
        try:
            raw = fn(**args)
        except Exception:  # noqa: BLE001 - nothing about an internal error goes to the model
            return self._deny(name, args, "tool_error", "The tool failed.")
        if rule.scope is not None:
            raw, dropped = self._scoped(rule, raw)
            if dropped is not None:
                return self._deny(name, args, "not_found", "No such object for this tenant.")
        safe_args = redact_deep(args) if cfg.masking else args
        result = redact_deep(raw) if cfg.masking else raw
        if rule.effect == "write":
            self.writes[name] = self.writes.get(name, 0) + 1
        return ToolOutcome(name, safe_args, True, "ok", "", result, "executed" if rule.effect == "write" else "none")

    def _scoped(self, rule: ToolRule, raw: Any) -> tuple[Any, bool | None]:
        """Keep only what belongs to this tenant. Returns (None, True) when one object was foreign (so: missing)."""
        sc = rule.scope
        assert sc is not None
        if sc.list_key and isinstance(raw, dict) and isinstance(raw.get(sc.list_key), list):
            rows = [r for r in raw[sc.list_key] if isinstance(r, dict) and r.get(sc.owner_field) == self.tenant]
            return {**raw, sc.list_key: rows, "count": len(rows)}, None
        if raw is None:
            return raw, None
        if isinstance(raw, dict) and raw.get(sc.owner_field) == self.tenant:
            return raw, None
        return None, True  # foreign, or no owner field at all: fail closed

    # ----- the answer
    def check_answer(self, text: str) -> AnswerCheck:
        """Run the model's final answer through the output guards before anyone sees it."""
        cfg = self.guard.config
        shown, kinds = text, [f.kind for f in find_pii(text)] if cfg.masking else []
        if cfg.masking:
            shown = redact(text)[0]
        results = [o.result for o in self.outcomes if o.allowed and o.result is not None]
        unsupported = unsupported_amounts(text, results, self.user_text) if cfg.numeric_fidelity else []
        withheld = bool(unsupported)
        if withheld:
            shown = WITHHELD.format(amounts=", ".join("₹" + a for a in unsupported))
        self.guard._log(
            self.tenant, "run_end",
            {
                "answer": shown,
                "withheld": withheld,
                "tool_calls": len(self.outcomes),
                "pii_masked": len(kinds),
                "unsupported_amounts": unsupported,
            },
            self.run_id,
        )  # fmt: skip
        return AnswerCheck(shown, sorted(set(kinds)), unsupported, withheld)
