"""Bootstrap findings passthrough tests (VULN-BOOTSTRAP fix): the bootstrap
execute payload may carry a structured ``findings`` array (same shape as
explore) so confirmed vulnerabilities reach the vulnerability library even when
bootstrap completes the project in one shot."""

from __future__ import annotations

import pytest

from sharp.dispatcher.contracts import validate_bootstrap_execute_payload


def _complete_payload(findings=None):
    data = {
        "fact": {"description": "目标已审计，确认 2 个漏洞"},
        "complete": {"description": "目标已拿下"},
    }
    if findings is not None:
        data["findings"] = findings
    return {"accepted": True, "data": data}


def test_findings_passthrough():
    findings = [
        {"title": "RCE", "severity": "critical", "evidence": "id → root"},
        {"title": "弱口令", "severity": "high"},
    ]
    kind, data = validate_bootstrap_execute_payload(_complete_payload(findings))
    assert kind == "complete"
    assert data["fact_description"]
    assert data["complete_description"]
    assert data["findings"] == findings


def test_no_findings_ok():
    kind, data = validate_bootstrap_execute_payload(_complete_payload())
    assert kind == "complete"
    assert "findings" not in data


def test_findings_must_be_list():
    with pytest.raises(ValueError):
        validate_bootstrap_execute_payload(_complete_payload(findings={"title": "x"}))
