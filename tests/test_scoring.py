from app.schemas import HistoryRecord, TCIssue, TCValidation
from app.scoring import decide, merge_risk, rule_risk


def recs(**counts):
    return [HistoryRecord(field_name=k, num_of_claims=v) for k, v in counts.items()]


def test_history_api_record():
    r = HistoryRecord.model_validate({"field_name": "imei", "num_of_claims": 2})
    assert r.num_of_claims == 2


def test_no_claims_is_low_risk(settings):
    r = rule_risk(recs(imei=0, id_number=0), settings)
    assert r.level == "low" and not r.hard_flag and r.history_score == 100


def test_imei_reuse_is_hard_flag(settings):
    r = rule_risk(recs(imei=1), settings)  # "imei" maps to imei_or_serial rule
    assert r.level == "high" and r.hard_flag and r.history_score == 0


def test_id_number_reuse_is_medium(settings):
    r = rule_risk(recs(id_number=2), settings)
    assert r.level == "medium" and not r.hard_flag and r.history_score == 60


def test_ai_can_raise_but_not_lower(settings):
    rule = rule_risk(recs(imei=1), settings)
    merged = merge_risk(rule, {"risk_level": "low"}, recs(imei=1))
    assert merged.level == "high" and merged.hard_flag

    rule = rule_risk(recs(imei=0), settings)
    merged = merge_risk(rule, {"risk_level": "high", "flags": ["same ID, different device"]}, recs(imei=0))
    assert merged.level == "high" and merged.history_score <= 20
    assert "ai: same ID, different device" in merged.flags


def tc_ok():
    return TCValidation(passed=True, compliance_score=100)


def test_clean_claim_is_approved(settings):
    risk = merge_risk(rule_risk(recs(imei=0), settings), {}, [])
    d = decide(96, tc_ok(), risk, [], settings)
    assert d.decision == "approve" and d.overall_score >= 90


def test_hard_flag_caps_score_and_rejects(settings):
    risk = merge_risk(rule_risk(recs(imei=1), settings), {}, recs(imei=1))
    d = decide(100, tc_ok(), risk, [], settings)
    assert d.decision == "reject" and d.overall_score == settings.hard_flag_score_cap


def test_high_tc_issue_rejects(settings):
    tc = TCValidation(passed=False, compliance_score=70,
                      issues=[TCIssue(rule="Qualifying devices", severity="high")])
    risk = merge_risk(rule_risk([], settings), {}, [])
    assert decide(95, tc, risk, [], settings).decision == "reject"
    settings.tc_high_issue_decision = "review"
    assert decide(95, tc, risk, [], settings).decision == "review"


def test_flags_prevent_auto_approve(settings):
    risk = merge_risk(rule_risk([], settings), {}, [])
    d = decide(100, tc_ok(), risk, ["low OCR confidence (0.50)"], settings)
    assert d.decision == "review"


def test_missing_tc_rules_goes_to_review_not_reject(settings):
    tc = TCValidation(passed=False, compliance_score=None,
                      issues=[TCIssue(rule="TC rules available", severity="high")])
    risk = merge_risk(rule_risk([], settings), {}, [])
    d = decide(100, tc, risk, ["no TC rules found for campaign"], settings)
    assert d.decision == "review"
