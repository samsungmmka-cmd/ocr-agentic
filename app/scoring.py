"""Deterministic risk rules, overall score and decision.

The LLM explains and can *raise* the risk level; it can never lower a rule
result. Scores and decisions are computed here so they are repeatable.
"""
from dataclasses import dataclass, field

from app.config import Settings
from app.schemas import HistoryRecord, RiskAssessment, TCValidation

LEVELS = ["low", "medium", "high"]


def max_level(*levels: str) -> str:
    return max((lvl for lvl in levels if lvl in LEVELS), key=LEVELS.index, default="low")


@dataclass
class RuleRisk:
    level: str = "low"
    hard_flag: bool = False
    flags: list[str] = field(default_factory=list)
    history_score: int = 100


def rule_risk(records: list[HistoryRecord], s: Settings) -> RuleRisk:
    rules = {k.lower(): v for k, v in s.risk_rules.items()}
    penalties = {"high": s.risk_penalty_high, "medium": s.risk_penalty_medium, "low": s.risk_penalty_low}
    out = RuleRisk()
    score = 100
    for r in records:
        if r.num_of_claims <= 0:
            continue
        level = rules.get(r.field_name.lower(), s.risk_default_level)
        out.level = max_level(out.level, level)
        out.flags.append(f"{r.field_name} used in {r.num_of_claims} previous claim(s) [{level}]")
        score -= penalties.get(level, s.risk_penalty_low)
        if level == "high":
            out.hard_flag = True
    out.history_score = max(0, score)
    return out


def merge_risk(rule: RuleRisk, ai: dict, records: list[HistoryRecord]) -> RiskAssessment:
    ai_level = str(ai.get("risk_level", "low")).lower()
    flags = list(rule.flags)
    flagged_fields = {r.field_name.lower() for r in records if r.num_of_claims > 0}
    for f in ai.get("flags") or []:
        # keep only AI findings that add something beyond the rule flags
        if f and not any(field in str(f).lower() for field in flagged_fields):
            flags.append(f"ai: {f}")
    level = max_level(rule.level, ai_level)
    history_score = rule.history_score
    # AI may only make things stricter.
    if LEVELS.index(level) > LEVELS.index(rule.level):
        history_score = min(history_score, {"medium": 60, "high": 20}.get(level, history_score))
    return RiskAssessment(
        level=level,
        hard_flag=rule.hard_flag,
        flags=flags,
        history=records,
        history_score=history_score,
        explanation=str(ai.get("explanation", "")),
    )


@dataclass
class Decision:
    overall_score: int
    decision: str
    reasons: list[str]


def decide(field_score: int | None, tc: TCValidation, risk: RiskAssessment,
           extra_flags: list[str], s: Settings) -> Decision:
    """Weighted score + rule-based decision.

    reject : hard history flag, high-severity TC failure (if configured), or low score
    approve: high score, TC passed, low risk, no flags
    review : everything in between
    """
    parts = [
        (field_score, s.weight_fields),
        (tc.compliance_score, s.weight_tc),
        (risk.history_score, s.weight_history),
    ]
    available = [(v, w) for v, w in parts if v is not None]
    total_w = sum(w for _, w in available) or 1
    score = round(sum(v * w for v, w in available) / total_w)

    reasons: list[str] = list(extra_flags)
    if risk.hard_flag:
        score = min(score, s.hard_flag_score_cap)
        reasons.append("hard history flag")
    # compliance_score None = rules missing (system issue) -> never auto-reject on TC
    high_tc = [i.rule for i in tc.issues if i.severity == "high"] if tc.compliance_score is not None else []
    if high_tc:
        reasons.append("TC high-severity failure: " + ", ".join(high_tc))
    elif not tc.passed:
        reasons.append("TC validation not passed")
    if risk.level != "low" and not risk.hard_flag:
        reasons.append(f"{risk.level} history risk")
    if score < s.reject_threshold:
        reasons.append(f"score below {s.reject_threshold}")

    if risk.hard_flag or score < s.reject_threshold:
        decision = "reject"
    elif high_tc and s.tc_high_issue_decision == "reject":
        decision = "reject"
    elif score >= s.approve_threshold and not reasons:
        decision = "approve"
    else:
        decision = "review"
    return Decision(overall_score=score, decision=decision, reasons=reasons)
