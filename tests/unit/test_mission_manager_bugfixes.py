"""TDD: MissionManager bug 修復測試 (C2/C3/H1/H2).

驗證：
  H1 — BLOCKED 轉換必須提供 blocker_detail
  H2 — update_task 直接賦值時 Artifact 路徑穿越防護必須生效
  C2 — _get_mission_path 不再獨立拋出 PermissionError
  C3 — memory_server/server.py 中不再有 role="base" (小寫)
"""

from __future__ import annotations

import inspect

import pytest
from memory_server.models.mission import TaskStatus
from memory_server.services.mission_manager import MissionManager
from pydantic import ValidationError


class _FakeIdentity:
    """測試用最小身分物件（忠實鏡射 AgentIdentity 的分面角色介面，ADR-0141）。

    mission_manager 拆分後改呼 ``is_privileged()``（不再讀扁平 ``.role``），故替身
    需同步依值路由出 ``human_role``／``agent_role`` 並提供該謂詞。
    """

    _HUMAN_ROLES = frozenset({"admin", "normal"})

    def __init__(self, agent_id: str, role: str, capabilities: list[str]) -> None:
        self.agent_id = agent_id
        self.role = role
        self.capabilities = capabilities
        self.human_role = role if role in self._HUMAN_ROLES else None
        self.agent_role = role if role not in self._HUMAN_ROLES else None

    def is_privileged(self) -> bool:
        """三元組 {Owner, Debugger, admin}（鏡射 AgentIdentity.is_privileged）。"""
        return self.human_role == "admin" or self.agent_role in ("Owner", "Debugger")


PRIVILEGED = _FakeIdentity("DebugAgent", "Debugger", ["can_supervise_dispatch"])
NORMAL = _FakeIdentity("NormalAgent", "Base", [])


def _reset(monkeypatch, tmp_path):
    """設置沙盒資料目錄並重置 resource proxy。"""
    monkeypatch.setenv("MEMORY_APP_DATA_DIR", str(tmp_path))
    from memory_server import config_utils
    config_utils._reset_resource_proxy_cache()


# ===========================================================================
# H1: BLOCKED 轉換必須提供 blocker_detail
# ===========================================================================


class TestH1BlockedRequiresBlockerDetail:
    """H1: 轉入 BLOCKED 狀態時，必須提供非空的 blocker_detail。"""

    @pytest.mark.asyncio
    async def test_blocked_without_detail_raises(self, tmp_path, monkeypatch):
        """無 blocker_detail 轉入 BLOCKED → ValueError。"""
        _reset(monkeypatch, tmp_path)
        manager = MissionManager()
        await manager.initialize_mission(
            identity=PRIVILEGED,
            goal="H1 test",
            initial_tasks=[{"id": "t1", "description": "task one"}],
        )
        with pytest.raises(ValueError, match="blocker_detail"):
            await manager.update_task(
                identity=PRIVILEGED,
                task_id="t1",
                status=TaskStatus.BLOCKED,
                blocker_detail=None,
            )

    @pytest.mark.asyncio
    async def test_blocked_with_empty_string_detail_raises(self, tmp_path, monkeypatch):
        """空字串 blocker_detail 轉入 BLOCKED → ValueError。"""
        _reset(monkeypatch, tmp_path)
        manager = MissionManager()
        await manager.initialize_mission(
            identity=PRIVILEGED,
            goal="H1 empty string test",
            initial_tasks=[{"id": "t1", "description": "task one"}],
        )
        with pytest.raises(ValueError, match="blocker_detail"):
            await manager.update_task(
                identity=PRIVILEGED,
                task_id="t1",
                status=TaskStatus.BLOCKED,
                blocker_detail="",
            )

    @pytest.mark.asyncio
    async def test_blocked_with_detail_succeeds(self, tmp_path, monkeypatch):
        """有 blocker_detail 轉入 BLOCKED → 成功，狀態正確更新。"""
        _reset(monkeypatch, tmp_path)
        manager = MissionManager()
        await manager.initialize_mission(
            identity=PRIVILEGED,
            goal="H1 success test",
            initial_tasks=[{"id": "t1", "description": "task one"}],
        )
        state = await manager.update_task(
            identity=PRIVILEGED,
            task_id="t1",
            status=TaskStatus.BLOCKED,
            blocker_detail="Waiting for external API response",
        )
        task = next(t for t in state.tasks if t.id == "t1")
        assert task.status == TaskStatus.BLOCKED
        assert task.blocker_detail == "Waiting for external API response"


# ===========================================================================
# H2: Artifact 路徑穿越防護在 update_task 直接賦值時必須生效
# ===========================================================================


class TestH2ArtifactValidationOnUpdate:
    """H2: update_task 直接賦值時，_check_secure_path 必須觸發。"""

    @pytest.mark.asyncio
    async def test_artifact_ref_path_traversal_blocked_on_update(
        self, tmp_path, monkeypatch
    ):
        """artifact_ref 含 '..' → update_task 應拋出 ValidationError 或 ValueError。"""
        _reset(monkeypatch, tmp_path)
        manager = MissionManager()
        await manager.initialize_mission(
            identity=PRIVILEGED,
            goal="H2 artifact_ref test",
            initial_tasks=[{"id": "t1", "description": "task"}],
        )
        await manager.update_task(
            identity=PRIVILEGED, task_id="t1", status=TaskStatus.DOING
        )
        with pytest.raises((ValueError, ValidationError)):
            await manager.update_task(
                identity=PRIVILEGED,
                task_id="t1",
                status=TaskStatus.DOING,
                artifact_ref="../../../etc/passwd",
            )

    @pytest.mark.asyncio
    async def test_artifact_refs_path_traversal_blocked_on_update(
        self, tmp_path, monkeypatch
    ):
        """artifact_refs 含 '..' → update_task 應拋出 ValidationError 或 ValueError。"""
        _reset(monkeypatch, tmp_path)
        manager = MissionManager()
        await manager.initialize_mission(
            identity=PRIVILEGED,
            goal="H2 artifact_refs test",
            initial_tasks=[{"id": "t1", "description": "task"}],
        )
        await manager.update_task(
            identity=PRIVILEGED, task_id="t1", status=TaskStatus.DOING
        )
        with pytest.raises((ValueError, ValidationError)):
            await manager.update_task(
                identity=PRIVILEGED,
                task_id="t1",
                status=TaskStatus.DOING,
                artifact_refs=["valid/path.py", "../escape/me"],
            )

    @pytest.mark.asyncio
    async def test_artifact_refs_absolute_path_blocked_on_update(
        self, tmp_path, monkeypatch
    ):
        """artifact_refs 含絕對路徑 → update_task 應拋出 ValidationError 或 ValueError。"""
        _reset(monkeypatch, tmp_path)
        manager = MissionManager()
        await manager.initialize_mission(
            identity=PRIVILEGED,
            goal="H2 absolute path test",
            initial_tasks=[{"id": "t1", "description": "task"}],
        )
        await manager.update_task(
            identity=PRIVILEGED, task_id="t1", status=TaskStatus.DOING
        )
        with pytest.raises((ValueError, ValidationError)):
            await manager.update_task(
                identity=PRIVILEGED,
                task_id="t1",
                status=TaskStatus.DOING,
                artifact_refs=["valid/path.py", "/etc/shadow"],
            )

    @pytest.mark.asyncio
    async def test_valid_artifact_refs_pass_on_update(self, tmp_path, monkeypatch):
        """合法的相對路徑 artifact_refs → update_task 應成功。"""
        _reset(monkeypatch, tmp_path)
        manager = MissionManager()
        await manager.initialize_mission(
            identity=PRIVILEGED,
            goal="H2 valid path test",
            initial_tasks=[{"id": "t1", "description": "task"}],
        )
        await manager.update_task(
            identity=PRIVILEGED, task_id="t1", status=TaskStatus.DOING
        )
        state = await manager.update_task(
            identity=PRIVILEGED,
            task_id="t1",
            status=TaskStatus.DONE,
            artifact_refs=["src/main.py", "tests/test_main.py"],
        )
        task = next(t for t in state.tasks if t.id == "t1")
        assert task.artifact_refs == ["src/main.py", "tests/test_main.py"]


# ===========================================================================
# （C3「server.py role 大小寫一致性」測試已隨 ADR-0116 移除——獨立 MCP 模式退役、
#   memory_server/server.py 已刪除，該回歸守護的標的不復存在。）
# ===========================================================================


# ===========================================================================
# C2: _get_mission_path 不再獨立拋出 PermissionError
# ===========================================================================


class TestC2NoDuplicatePermissionCheck:
    """C2: _get_mission_path 不再獨立校驗權限（由 _resolve_target_agent_id 集中處理）。"""

    def test_get_mission_path_has_no_permission_error(self):
        """_get_mission_path 的源碼不應有 PermissionError raise。"""
        src = inspect.getsource(MissionManager._get_mission_path)
        assert "PermissionError" not in src, (
            "C2 修復：_get_mission_path 不應獨立拋出 PermissionError，"
            "權限檢查由 _resolve_target_agent_id 集中處理"
        )

    def test_resolve_target_agent_id_still_enforces_permission(self, tmp_path, monkeypatch):
        """_resolve_target_agent_id 仍應拒絕無權限的跨身分存取（權限檢查未丟失）。"""
        _reset(monkeypatch, tmp_path)
        manager = MissionManager()
        no_cap_identity = _FakeIdentity("RegularAgent", "Base", [])
        with pytest.raises(PermissionError):
            manager._resolve_target_agent_id(no_cap_identity, "DispatchAgent")  # noqa: SLF001
