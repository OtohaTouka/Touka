"""Adversarial-review follow-up (2026-06-06): cross-user IDOR on soul/context
endpoints + completion of the #7 path-sandbox migration in soul_io / custom_io.

Two finding classes, both surfaced by the DA11–DA16 adversarial review:

  (A) IDOR — debug.py context-snapshot (read/export) and souls.py update_soul gated
      ONLY on `role in {Owner,Debugger,admin}`, then built an `AgentIdentity` from an
      attacker-supplied agent_id/soul_id and acted on it with NO ownership check → a
      non-admin Owner could read/export/overwrite ANOTHER user's soul/context. Fix
      adds `resource_manager.is_owner(target, caller)` (admin/Debugger bypass inside).

  (B) Sandbox — soul_io.py (read/write/delete used str.startswith with an UN-resolved
      base; backup/undo/exists had NO check) and custom_io.py (write used startswith;
      read/exists had NO check). All now route through `_safe_target` →
      `validate_no_symlink_escape` (resolved `is_relative_to`), killing the
      sibling-prefix bypass (`/soul/AgentX-evil` startswith `/soul/AgentX`).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from src.main import app
from src.models.custom_models import CustomModelRenameRequest
from src.models.identity import AgentIdentity
from src.services.identity_router import get_verified_identity
from src.services.resource_manager import resource_manager
from src.utils import custom_io, soul_io

from tests.support.zone_records import expect_zone_refusal

OWNER = AgentIdentity.for_role(agent_id="ownerAgent", role="Owner", is_human=False, owned_agents=[])
ADMIN = AgentIdentity.for_role(agent_id="AdminUser", role="admin", is_human=True)


@pytest.fixture
def owner_client():
    app.dependency_overrides[get_verified_identity] = lambda: OWNER
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ===========================================================================
# (A) IDOR — debug context-snapshot + souls update_soul
# ===========================================================================


class TestContextSnapshotIDOR:
    def test_read_cross_owner_denied_and_service_not_reached(self, owner_client, monkeypatch):
        monkeypatch.setattr(resource_manager, "is_owner", lambda target, caller: False)
        reached = {"v": False}

        def _spy(target):
            reached["v"] = True
            return {"blocks": ["SECRET"]}

        monkeypatch.setattr(
            "src.services.context_snapshot_service.get_context_snapshot", _spy
        )
        # 2026-07-07：拒絕由 200＋空 blocks 改 403 fail-loud（與 export 一致）——
        # 靜默空回應曾把 admin 被拒誤診成「組裝壞掉」；安全不變（無洩漏、不觸 service）。
        r = owner_client.get("/debug/context-snapshot?agent_id=victimAgent")
        assert r.status_code == 403
        assert "SECRET" not in r.text  # no leak
        assert reached["v"] is False  # ownership gate blocked before the service

    def test_read_owned_allowed(self, owner_client, monkeypatch):
        monkeypatch.setattr(resource_manager, "is_owner", lambda target, caller: True)

        async def _fake_snap(*_a, **_k):  # ADR-0133：service 改為 async(identity, base_ctx)
            return {"blocks": ["ok"]}

        monkeypatch.setattr(
            "src.services.context_snapshot_service.get_context_snapshot", _fake_snap
        )
        r = owner_client.get("/debug/context-snapshot?agent_id=ownerAgent")
        assert r.status_code == 200
        assert r.json() == {"blocks": ["ok"]}

    def test_export_cross_owner_denied(self, owner_client, monkeypatch):
        monkeypatch.setattr(resource_manager, "is_owner", lambda target, caller: False)
        wrote = {"v": False}
        monkeypatch.setattr(
            resource_manager,
            "write_file_text",
            lambda *a, **k: wrote.__setitem__("v", True),
        )
        r = owner_client.get("/debug/context-snapshot/export?agent_id=victimAgent")
        assert r.status_code == 403
        assert wrote["v"] is False  # nothing exported to disk

    def test_export_owned_allowed(self, owner_client, monkeypatch, tmp_path):
        monkeypatch.setattr(resource_manager, "is_owner", lambda target, caller: True)

        async def _fake_snap(*_a, **_k):  # ADR-0133：service 改為 async(identity, base_ctx)
            return {"blocks": []}

        monkeypatch.setattr(
            "src.services.context_snapshot_service.get_context_snapshot", _fake_snap
        )
        monkeypatch.setattr(
            "src.services.context_snapshot_service.render_snapshot_markdown",
            lambda snap: "MARKDOWN",
        )
        monkeypatch.setattr(resource_manager, "get_base_path", lambda: str(tmp_path))
        monkeypatch.setattr(resource_manager, "write_file_text", lambda *a, **k: None)
        r = owner_client.get("/debug/context-snapshot/export?agent_id=ownerAgent")
        assert r.status_code == 200
        assert "MARKDOWN" in r.text


class TestSoulUpdateIDOR:
    def test_update_cross_owner_denied(self, owner_client, monkeypatch):
        monkeypatch.setattr(resource_manager, "is_owner", lambda target, caller: False)
        wrote = {"v": False}
        # C1（soul-crud repair）：update_soul 寫入已改走 soul_io 受認可通道 → spy 對象
        # 同步遷移（斷言不變：拒絕時完全不觸任何寫入）。
        monkeypatch.setattr(
            soul_io, "write_soul_file", lambda *a, **k: wrote.__setitem__("v", True) or True
        )
        monkeypatch.setattr(
            soul_io, "write_soul_json", lambda *a, **k: wrote.__setitem__("v", True) or True
        )
        r = owner_client.post("/souls/victimSoul", json={"persona": "pwned"})
        assert r.status_code == 403
        assert wrote["v"] is False  # no cross-owner write occurred

    def test_update_self_access_allowed_even_if_is_owner_false(self, monkeypatch):
        """Self-access (caller.agent_id == soul_id) must bypass is_owner so an agent
        is never locked out of its own soul by the autonomy-grace window."""
        selfid = AgentIdentity.for_role(agent_id="selfSoul", role="Owner", is_human=False)
        app.dependency_overrides[get_verified_identity] = lambda: selfid
        # is_owner would DENY (False), but self-access must still pass the gate; route
        # short-circuits at the vault step (get_vault_root None) → not a 403.
        monkeypatch.setattr(resource_manager, "is_owner", lambda target, caller: False)
        monkeypatch.setattr(resource_manager, "get_vault_root", lambda kind, ident: None)
        with TestClient(app) as c:
            r = c.post("/souls/selfSoul", json={"persona": "mine"})
        app.dependency_overrides.clear()
        assert r.status_code != 403  # passed ownership gate via self-access


# ===========================================================================
# (B) Sandbox completion — soul_io + custom_io (#7-class, sibling-prefix)
# ===========================================================================

IDENT = AgentIdentity.for_role(agent_id="agentX", role="Base")
TRAVERSAL = "../agentX-evil/secret.txt"


class TestSoulIoSandbox:
    @pytest.fixture
    def base(self, tmp_path, monkeypatch):
        b = tmp_path / "soul" / "agentX"
        b.mkdir(parents=True)
        evil = tmp_path / "soul" / "agentX-evil"
        evil.mkdir(parents=True)
        (evil / "secret.txt").write_text("SECRET", encoding="utf-8")
        monkeypatch.setattr(
            "src.utils.soul_io.resource_manager.get_vault_path",
            lambda kind, identity: str(b) if kind == "soul" else None,
        )
        return b

    def test_read_blocks_sibling_prefix_traversal(self, base):
        assert soul_io.read_soul_file(IDENT, TRAVERSAL) is None

    def test_write_blocks_traversal(self, base, tmp_path):
        assert soul_io.write_soul_file(IDENT, "../agentX-evil/pwned.txt", "x") is False
        assert not (tmp_path / "soul" / "agentX-evil" / "pwned.txt").exists()

    def test_delete_blocks_traversal(self, base, tmp_path):
        assert soul_io.delete_soul_file(IDENT, TRAVERSAL) is False
        assert (tmp_path / "soul" / "agentX-evil" / "secret.txt").exists()  # untouched

    def test_backup_blocks_traversal(self, base):
        assert soul_io.backup_soul_file(IDENT, TRAVERSAL) is False

    def test_undo_blocks_traversal(self, base):
        assert soul_io.undo_soul_edit(IDENT, TRAVERSAL) is False

    def test_exists_blocks_traversal(self, base):
        assert soul_io.soul_file_exists(IDENT, TRAVERSAL) is False

    def test_legitimate_read_write_roundtrip(self, base):
        assert soul_io.write_soul_file(IDENT, "soul.md", "hello") is True
        assert soul_io.read_soul_file(IDENT, "soul.md") == "hello"
        assert soul_io.soul_file_exists(IDENT, "soul.md") is True


class TestCustomIoSandbox:
    @pytest.fixture
    def cdir(self, tmp_path, monkeypatch):
        d = tmp_path / "custom" / "modelX"
        d.mkdir(parents=True)
        evil = tmp_path / "custom" / "modelX-evil"
        evil.mkdir(parents=True)
        (evil / "secret.txt").write_text("SECRET", encoding="utf-8")
        monkeypatch.setattr("src.utils.custom_io.get_custom_dir", lambda identity: d)
        return d

    def test_write_blocks_traversal(self, cdir, tmp_path):
        assert custom_io.write_custom_file(IDENT, "../modelX-evil/pwned.txt", "x") is False
        assert not (tmp_path / "custom" / "modelX-evil" / "pwned.txt").exists()

    def test_read_blocks_traversal(self, cdir):
        # read_custom_file previously had NO boundary check at all
        assert custom_io.read_custom_file(IDENT, "../modelX-evil/secret.txt") is None

    def test_exists_blocks_traversal(self, cdir):
        assert custom_io.custom_file_exists(IDENT, "../modelX-evil/secret.txt") is False

    def test_legitimate_roundtrip(self, cdir):
        assert custom_io.write_custom_file(IDENT, "weights.json", "{}") is True
        assert custom_io.read_custom_file(IDENT, "weights.json") == "{}"
        assert custom_io.custom_file_exists(IDENT, "weights.json") is True


class TestCustomIoMoveSandbox:
    """move_custom_dir was the ONLY custom_io function not routed through the
    sandbox guard — a non-admin Owner could rename a model to ``../../escape`` and
    shutil.move() the config-bearing dir OUT of <app_data>/custom. Both old & new
    paths must now stay inside the custom base (is_relative_to)."""

    @pytest.fixture
    def base(self, zone_data_root):
        # Nested so a "../../escape" target still lands inside tmp_path for assertions.
        b = zone_data_root / "custom"
        b.mkdir()
        old = b / "modelX"
        old.mkdir()
        (old / "config.json").write_text("{}", encoding="utf-8")
        return b

    def test_move_blocks_traversal(self, base, tmp_path):
        with expect_zone_refusal("ZoneSegmentError"):
            assert custom_io.move_custom_dir("modelX", "../../escape") is False
        # Source untouched; nothing materialised outside the sandbox.
        assert (base / "modelX" / "config.json").exists()
        assert not (tmp_path / "escape").exists()

    def test_move_blocks_dotdot_single_level(self, base, tmp_path):
        # ".." passes the new_id char-pattern but must be caught by the path guard.
        with expect_zone_refusal("ZoneSegmentError"):
            assert custom_io.move_custom_dir("modelX", "..") is False
        assert (base / "modelX").exists()

    def test_move_blocks_traversal_old_id(self, base):
        # An escaping OLD id is rejected too (defense on both sides).
        with expect_zone_refusal("ZoneSegmentError"):
            assert custom_io.move_custom_dir("../../etc", "modelY") is False
        assert not (base / "modelY").exists()

    def test_legitimate_rename_works(self, base):
        assert custom_io.move_custom_dir("modelX", "modelY") is True
        assert not (base / "modelX").exists()
        assert (base / "modelY" / "config.json").exists()


class TestCustomModelRenameRequestValidation:
    """Defense-in-depth: the rename request model must reject path-separator /
    traversal new_id values before they ever reach move_custom_dir."""

    @pytest.mark.parametrize(
        "bad_id",
        [
            "../../../../some/target/path", "../escape", "a/b", "a\\b", "", "foo/../bar",
            "a b", "中文/路徑",
        ],
    )
    def test_traversal_new_id_rejected(self, bad_id):
        with pytest.raises(ValidationError):
            CustomModelRenameRequest(new_id=bad_id)

    # 建立時可以用中文 ID，改名收同樣的名字（舊審查報告丙-2）
    @pytest.mark.parametrize(
        "good_id", ["modelY", "my_model-2.0", "Model_42", "a.b-c", "新模型", "助手_2"]
    )
    def test_legitimate_new_id_accepted(self, good_id):
        assert CustomModelRenameRequest(new_id=good_id).new_id == good_id
