"""G05（守衛涵蓋缺口待辦）— 靈魂步升 AUDIT 稽核軌跡（記操作者與對象、不記密碼）。

缺口背景（見 docs/architecture/proposals/guard-coverage-gap-backlog.md · G05）：
    ADR-0142 D2 的 `_verify_admin_stepup`（souls.py）成功／失敗皆須留 AUDIT log
    （被竊 session 對靈魂資料的敏感變更嘗試必須可追溯），且**不得記錄密碼本身**。

    此不變式**先前零測試**：`test_soul_admin_stepup.py` 只驗步升的授權行為、對 AUDIT
    log 零 caplog 斷言（AUDIT 只在 docstring 提及）；對照組 admin.py 的同類 AUDIT 承諾
    卻有 `test_sec_batch_gamma::test_admin_create_user_emits_audit_log` 源碼級釘住。
    → 刪掉兩行 `logger.warning` 或誤把密碼加進格式字串，全套件照綠。

本檔補上 caplog 回歸鎖：兩分支各斷言 AUDIT log 含操作者＋對象，且任一 log 不含密碼值。
"""

from __future__ import annotations

import logging

import pytest
from src.core.exceptions import ToukaException
from src.models.identity import AgentIdentity
from src.routers.souls import _verify_admin_stepup
from src.services.auth_service import AuthService

_PW = "StepUpAudit123!"
_WRONG_PW = "WrongStepUp999!"
_TARGET = "SomeSoul"
_ACTION = "delete"


def _wipe_users(auth: AuthService) -> None:
    auth._atomic_users_write(lambda users: users.clear())  # noqa: SLF001


@pytest.fixture
def _admin_identity():
    """建立真人 admin（沙箱 users.json）並回傳其步升身分。測後清空。"""
    auth = AuthService()
    _wipe_users(auth)
    user = auth.create_user("stepup-admin@test.com", _PW, role="admin")
    identity = AgentIdentity.for_role(user.id, "admin", is_human=True)
    yield identity, user.id
    _wipe_users(auth)


def _no_password_in(caplog: pytest.LogCaptureFixture) -> bool:
    return all(
        _PW not in r.getMessage() and _WRONG_PW not in r.getMessage()
        for r in caplog.records
    )


def test_stepup_success_emits_audit_with_operator_and_target(_admin_identity, caplog):
    """步升成功：AUDIT log 含操作者 id ＋對象，且不含密碼。"""
    identity, uid = _admin_identity
    with caplog.at_level(logging.WARNING):
        _verify_admin_stepup(identity, _PW, _ACTION, _TARGET)  # 不拋＝成功

    audit = [r.getMessage() for r in caplog.records if "AUDIT" in r.getMessage()]
    assert audit, "步升成功必須留 AUDIT log（可追溯）"
    joined = " ".join(audit)
    assert uid in joined and _TARGET in joined and _ACTION in joined
    assert _no_password_in(caplog), "AUDIT log 絕不得含密碼值"


def test_stepup_failure_emits_audit_with_operator_and_target(_admin_identity, caplog):
    """步升失敗（錯密碼）：拋 AUTH_REAUTH_FAILED，留 AUDIT FAILED log（操作者＋對象、不含密碼）。"""
    identity, uid = _admin_identity
    with caplog.at_level(logging.WARNING), pytest.raises(ToukaException):
        _verify_admin_stepup(identity, _WRONG_PW, _ACTION, _TARGET)

    audit = [r.getMessage() for r in caplog.records if "AUDIT" in r.getMessage()]
    assert audit, "步升失敗必須留 AUDIT log（被竊 session 的攻擊嘗試須可追溯）"
    joined = " ".join(audit)
    assert "FAILED" in joined and uid in joined and _TARGET in joined
    assert _no_password_in(caplog), "AUDIT log 絕不得含密碼值（含失敗嘗試的錯誤密碼）"
