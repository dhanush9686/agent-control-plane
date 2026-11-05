"""The rules file: what a team writes to say which tools their agent may call, and under what conditions.

Analogy: the checklist taped to the terminal. Nothing in it is code; the engine reads it and enforces it.

    version: 1
    budget: {max_calls: 8}
    masking: true
    numeric_fidelity: true
    tools:
      get_payment:
        effect: read
        scope: {owner_field: merchant_id}
      propose_refund:
        effect: write
        max_per_session: 2
        require_named: {payment_id: id, amount_paise: amount_paise}
        limits: {amount_paise: {min: 1, max: 5000000}}
        approval: {required: true, tiers: [{field: amount_paise, above: 1000000, tier: senior}]}

Unknown keys are rejected, so a typo in a rule is an error at load time, not a silently missing control.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Scope(_Strict):
    """The result of this tool names its owner in `owner_field`; a result owned by someone else looks missing."""

    owner_field: str
    list_key: str | None = None  # when the result is {"<list_key>": [...]}, foreign rows are dropped instead


class Limit(_Strict):
    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def _ordered(self) -> Limit:
        if self.min is None and self.max is None:
            raise ValueError("a limit needs min, max or both")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min is greater than max")
        return self


class Tier(_Strict):
    field: str
    above: float
    tier: str


class Approval(_Strict):
    required: bool = True
    tiers: list[Tier] = Field(default_factory=list)


Named = Literal["id", "amount_paise"]


class ToolRule(_Strict):
    effect: Literal["read", "write"] = "read"
    scope: Scope | None = None
    max_per_session: int | None = Field(default=None, ge=1)
    # argument name -> how the user's own message must contain it
    #   id            the value must appear verbatim in the message (a payment id the requester typed)
    #   amount_paise  the value, converted to rupees, must be an amount the message states
    require_named: dict[str, Named] = Field(default_factory=dict)
    limits: dict[str, Limit] = Field(default_factory=dict)
    approval: Approval | None = None

    @model_validator(mode="after")
    def _write_rules(self) -> ToolRule:
        if self.effect == "read" and (self.approval or self.require_named or self.max_per_session):
            raise ValueError("approval, require_named and max_per_session apply to write tools only")
        if self.effect == "write" and self.scope:
            raise ValueError("scope applies to read tools (what comes back); write tools use require_named")
        return self


class GuardConfig(_Strict):
    version: Literal[1] = 1
    budget: dict[Literal["max_calls"], int] = Field(default_factory=lambda: {"max_calls": 8})
    masking: bool = True
    numeric_fidelity: bool = True
    tools: dict[str, ToolRule]

    @property
    def max_calls(self) -> int:
        return int(self.budget.get("max_calls", 8))


class ConfigError(ValueError):
    """The rules file is invalid. The message lists every problem, with its location in the file."""


def parse_config(data: Any) -> GuardConfig:
    try:
        return GuardConfig.model_validate(data)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(map(str, x['loc'])) or 'file'}: {x['msg']}" for x in e.errors())
        raise ConfigError(f"Invalid rules file: {problems}") from None


def load_config(path: str | Path) -> GuardConfig:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"Could not read the rules file as YAML: {e}") from None
    return parse_config(data)
