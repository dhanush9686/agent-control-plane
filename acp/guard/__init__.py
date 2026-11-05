"""Put guardrails in front of your own agent's tools, with the rules in a config file.

    from acp.guard import Guard

    guard = Guard.from_file("guard.yaml", audit_db="audit.db")
    guard.register("get_payment", get_payment).register("propose_refund", propose_refund)
    session = guard.session(tenant="m_acme", user_text="Refund pay_A001 ₹500")
    safe_result = session.call("get_payment", payment_id="pay_A001").for_model()
    shown = session.check_answer(model_answer).text

See docs/GUARD_SDK.md for the rules file and a worked example.
"""

from acp.guard.config import ConfigError, GuardConfig, load_config, parse_config
from acp.guard.engine import AnswerCheck, ApprovalRequest, Guard, Session, ToolOutcome

__all__ = [
    "AnswerCheck", "ApprovalRequest", "ConfigError", "Guard", "GuardConfig", "Session", "ToolOutcome",
    "load_config", "parse_config",
]  # fmt: skip
