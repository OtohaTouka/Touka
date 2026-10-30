"""ADR-0110 — 使用者自助 GDPR 完整資料匯出（TDD）。

Covers:
  - ToukaMemory ``export_facts`` 動作：JSON 形狀、agent 範圍化、include_archived
  - ``_sanitize_user``：白名單脫敏（排除密碼雜湊/權杖）
  - ``create_encrypted_export``：加密 ZIP roundtrip（解密驗內容、僅本人對話、事實、
    個人硬碟檔、profile 無機密）、記憶不可用時優雅降級
  - 個人硬碟不跟隨連結（ADR-0293）：根是連結整個略過、裡面的連結不打包；data 根換到
    ``zone_data_root``（``tmp_path``），不碰 ``data/``
  - ``POST /auth/export-my-data`` 端點：需登入、密碼錯誤 401、密語過短 422、happy path
"""

from __future__ import annotations

import json
import shutil

import memory_server.tools as mtmod
import pyzipper
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.models.user import User
from src.routers.auth import get_current_user
from src.services import gdpr_export_service as gx
from src.services.auth_service import AuthService

from tests.support.links import LINK_FOLLOWED, make_dir_link

_PHRASE = "passphrase-123456"  # ≥12 字


def _clear():
    app.dependency_overrides.clear()


async def _empty():
    return []


def _drive(zone_data_root, agent="A"):
    """沒有靈魂資料夾的 agent，個人硬碟在 ``custom/<id>/drive``（和 agent_drive 相同）。"""
    folder = zone_data_root / "custom" / agent / "drive"
    folder.mkdir(parents=True)
    return folder


def _outside(tmp_path):
    """連結的另一端：根外面的假設定資料夾。"""
    folder = tmp_path.resolve() / "fake_config"
    folder.mkdir()
    (folder / "users.json").write_text("TOUKA_AUTH_SECRET=leak", encoding="utf-8")
    return folder


# ── ToukaMemory: export_facts 動作 ─────────────────────────────────────────────

async def test_export_facts_action_scoped_and_archived(monkeypatch):
    captured = {}

    class FakeFactual:
        def query_facts(self, identity, include_archived=False, **_kw):
            captured["agent_id"] = identity.agent_id
            captured["archived"] = include_archived
            return [{"id": "f1", "content": "hello"}]

    class FakeManager:
        def get_factual_store(self):
            return FakeFactual()

    monkeypatch.setattr(mtmod, "get_manager", lambda: FakeManager())
    raw = await mtmod.memory_tools(action="export_facts", identity=AgentIdentity(agent_id="AgentX"))
    data = json.loads(raw)

    assert data["agent_id"] == "AgentX"
    assert data["count"] == 1
    assert data["facts"][0]["content"] == "hello"
    assert captured["agent_id"] == "AgentX"  # 嚴格範圍化到該 agent
    assert captured["archived"] is True  # 含已封存（完整匯出）


# ── service: 脫敏白名單 ─────────────────────────────────────────────────────────

def test_sanitize_user_excludes_secrets():
    user = User(
        id="U1", email="u1@touka.io", hashed_password="TOPSECRET",
        is_temp_password=True, refresh_token_jti="jti-123",
        previous_refresh_jti="old", owned_agents=["A"], display_name="Alice",
    )
    out = gx._sanitize_user(user)
    for leaked in (
        "hashed_password", "is_temp_password", "refresh_token_jti",
        "previous_refresh_jti", "refresh_jti_rotated_at", "refresh_token_exp",
    ):
        assert leaked not in out, f"機密欄位 {leaked} 不應出現在匯出"
    assert out["email"] == "u1@touka.io"
    assert out["owned_agents"] == ["A"]


# ── service: 加密 ZIP roundtrip ────────────────────────────────────────────────

async def test_create_encrypted_export_roundtrip(zone_data_root, monkeypatch):
    user = User(
        id="U1", email="u1@touka.io", hashed_password="secret-hash",
        owned_agents=["A"], display_name="Alice", bio="hi",
    )

    async def fake_sessions():
        return [
            {"id": "s1", "agent_id": "A", "title": "mine", "messages": {}},
            {"id": "s2", "agent_id": "B", "title": "other-user", "messages": {}},
        ]

    async def fake_projects():
        return [
            {"id": "p1", "owner_user_id": "U1", "name": "P1"},
            {"id": "p2", "owner_user_id": "OTHER", "name": "P2"},
        ]

    monkeypatch.setattr(gx.session_service, "get_all_sessions", fake_sessions)
    monkeypatch.setattr(gx.session_service, "get_all_projects", fake_projects)

    (_drive(zone_data_root) / "note.txt").write_text("agent A file", encoding="utf-8")
    (_drive(zone_data_root, "B") / "other.txt").write_text("not mine", encoding="utf-8")
    monkeypatch.setattr(gx, "find_avatar", lambda _uid: None)

    async def fake_facts(identity):
        return {"agent_id": identity.agent_id, "count": 2,
                "facts": [{"content": "f1"}, {"content": "f2"}]}

    enc = await gx.create_encrypted_export(user, _PHRASE, facts_fn=fake_facts)
    try:
        with pyzipper.AESZipFile(str(enc)) as zf:
            zf.setpassword(_PHRASE.encode())
            names = set(zf.namelist())
            assert {
                "profile.json", "conversations.json", "projects.json",
                "manifest.json", "README.txt", "agents/A/facts.json",
                "agents/A/files/note.txt",
            } <= names

            profile = json.loads(zf.read("profile.json"))
            assert "hashed_password" not in profile
            assert profile["email"] == "u1@touka.io"

            convs = json.loads(zf.read("conversations.json"))
            assert [c["id"] for c in convs] == ["s1"]  # 僅本人自有 agent，B 的對話被排除

            projs = json.loads(zf.read("projects.json"))
            assert [p["id"] for p in projs] == ["p1"]  # 僅本人擁有專案

            facts = json.loads(zf.read("agents/A/facts.json"))
            assert facts["count"] == 2

            assert zf.read("agents/A/files/note.txt").decode() == "agent A file"
            assert not any(name.startswith("agents/B/") for name in names)  # 別人的 agent

            manifest = json.loads(zf.read("manifest.json"))
            assert manifest["counts"]["conversations"] == 1
            assert manifest["counts"]["facts"] == 2
    finally:
        enc.unlink(missing_ok=True)


async def test_export_includes_owner_user_id_bare_model_conversations(zone_data_root, monkeypatch):
    """G-2（ADR-0110 × ADR-0132）：使用者「擁有」的裸模型對話（owner_user_id==user.id 且
    agent_id 非 owned agent，如與 gemini-2.5-flash 直聊）必須納入 GDPR 匯出；他人擁有的
    （owner_user_id 不符）仍排除。防回歸：匯出過濾須是 agent_id∈owned ∪ owner_user_id==user.id。"""
    user = User(id="U1", email="u1@touka.io", hashed_password="h", owned_agents=["A"])

    async def fake_sessions():
        return [
            {"id": "s1", "agent_id": "A", "title": "owned-agent"},
            {"id": "s2", "agent_id": "B", "title": "other-user-agent"},
            {"id": "s3", "agent_id": "gemini-2.5-flash", "owner_user_id": "U1"},
            {"id": "s4", "agent_id": "gemini-2.5-flash", "owner_user_id": "OTHER"},
        ]

    monkeypatch.setattr(gx.session_service, "get_all_sessions", fake_sessions)
    monkeypatch.setattr(gx.session_service, "get_all_projects", _empty)
    monkeypatch.setattr(gx, "find_avatar", lambda _uid: None)

    async def no_facts(_i):
        return None

    enc = await gx.create_encrypted_export(user, _PHRASE, facts_fn=no_facts)
    try:
        with pyzipper.AESZipFile(str(enc)) as zf:
            zf.setpassword(_PHRASE.encode())
            convs = json.loads(zf.read("conversations.json"))
            ids = [c["id"] for c in convs]
            # s1=自有 agent、s3=本人擁有的裸模型對話 → 納入；s2/s4=非本人 → 排除
            assert ids == ["s1", "s3"], f"expected owned-agent + owner-scoped bare-model, got {ids}"
            manifest = json.loads(zf.read("manifest.json"))
            assert manifest["counts"]["conversations"] == 2
    finally:
        enc.unlink(missing_ok=True)


async def test_export_skips_symlink_escape(zone_data_root, tmp_path, monkeypatch):
    """複審 High：個人硬碟內指向外部機密的連結不得被打包（防 .env/users.db 外洩；ADR-0293）。"""
    pdir = _drive(zone_data_root)
    (pdir / "ok.txt").write_text("safe-content", encoding="utf-8")
    make_dir_link(pdir / "leak", _outside(tmp_path))

    monkeypatch.setattr(gx.session_service, "get_all_sessions", _empty)
    monkeypatch.setattr(gx.session_service, "get_all_projects", _empty)
    monkeypatch.setattr(gx, "find_avatar", lambda _uid: None)

    async def no_facts(_identity):
        return None

    user = User(id="U3", email="u3@touka.io", hashed_password="h", owned_agents=["A"])
    enc = await gx.create_encrypted_export(user, _PHRASE, facts_fn=no_facts)
    try:
        with pyzipper.AESZipFile(str(enc)) as zf:
            zf.setpassword(_PHRASE.encode())
            names = set(zf.namelist())
            assert not any("users.json" in name for name in names), LINK_FOLLOWED
            assert "agents/A/files/ok.txt" in names  # 正常檔仍匯出
            manifest = json.loads(zf.read("manifest.json"))
            skipped = {"agent": "A", "vault": "files", "reason": "symlink_escape", "count": 1}
            assert skipped in manifest["skipped"]
    finally:
        enc.unlink(missing_ok=True)


async def test_export_skips_a_linked_drive_root(zone_data_root, tmp_path, monkeypatch):
    """個人硬碟根本身是連結：整個略過、記入 manifest，連結另一端一個檔都不打包（ADR-0293）。"""
    (zone_data_root / "custom" / "A").mkdir(parents=True)
    make_dir_link(zone_data_root / "custom" / "A" / "drive", _outside(tmp_path))
    monkeypatch.setattr(gx.session_service, "get_all_sessions", _empty)
    monkeypatch.setattr(gx.session_service, "get_all_projects", _empty)
    monkeypatch.setattr(gx, "find_avatar", lambda _uid: None)

    async def no_facts(_i):
        return None

    user = User(id="U4", email="u4@touka.io", hashed_password="h", owned_agents=["A"])
    enc = await gx.create_encrypted_export(user, _PHRASE, facts_fn=no_facts)
    try:
        with pyzipper.AESZipFile(str(enc)) as zf:
            zf.setpassword(_PHRASE.encode())
            names = set(zf.namelist())
            assert not any(name.startswith("agents/A/files/") for name in names), LINK_FOLLOWED
            manifest = json.loads(zf.read("manifest.json"))
            skipped = {"agent": "A", "vault": "files", "reason": "root_escapes_app_data"}
            assert skipped in manifest["skipped"]
    finally:
        enc.unlink(missing_ok=True)


async def test_a_folder_swapped_for_a_link_after_collection_is_not_packed(
    zone_data_root, tmp_path, monkeypatch
):
    """收集之後、寫 zip 之前把資料夾換成連結：另一端的檔案不打包（ADR-0293；舊版會跟隨）。"""
    pdir = _drive(zone_data_root)
    (pdir / "sub").mkdir()
    (pdir / "sub" / "a.txt").write_text("mine", encoding="utf-8")
    fake = _outside(tmp_path)
    (fake / "a.txt").write_text("TOUKA_AUTH_SECRET=leak", encoding="utf-8")
    write_zip = gx._write_plaintext_zip

    def swap_then_write(*args):
        shutil.rmtree(pdir / "sub")
        make_dir_link(pdir / "sub", fake)
        return write_zip(*args)

    monkeypatch.setattr(gx, "_write_plaintext_zip", swap_then_write)
    monkeypatch.setattr(gx.session_service, "get_all_sessions", _empty)
    monkeypatch.setattr(gx.session_service, "get_all_projects", _empty)
    monkeypatch.setattr(gx, "find_avatar", lambda _uid: None)

    async def no_facts(_i):
        return None

    user = User(id="U6", email="u6@touka.io", hashed_password="h", owned_agents=["A"])
    enc = await gx.create_encrypted_export(user, _PHRASE, facts_fn=no_facts)
    try:
        with pyzipper.AESZipFile(str(enc)) as zf:
            zf.setpassword(_PHRASE.encode())
            packed = [zf.read(name) for name in zf.namelist()]
    finally:
        enc.unlink(missing_ok=True)
    assert not any(b"leak" in content for content in packed), LINK_FOLLOWED


async def test_export_skips_oversize_file(zone_data_root, monkeypatch):
    """複審 Med（防 OOM DoS）：超過單檔上限者略過、記入 manifest（不入包）。"""
    pdir = _drive(zone_data_root)
    (pdir / "small.txt").write_text("ok", encoding="utf-8")
    (pdir / "big.bin").write_bytes(b"x" * 50)

    monkeypatch.setattr(gx, "_MAX_FILE_BYTES", 10)  # 縮小上限以測試
    monkeypatch.setattr(gx.session_service, "get_all_sessions", _empty)
    monkeypatch.setattr(gx.session_service, "get_all_projects", _empty)
    monkeypatch.setattr(gx, "find_avatar", lambda _uid: None)

    async def no_facts(_i):
        return None

    user = User(id="U5", email="u5@touka.io", hashed_password="h", owned_agents=["A"])
    enc = await gx.create_encrypted_export(user, _PHRASE, facts_fn=no_facts)
    try:
        with pyzipper.AESZipFile(str(enc)) as zf:
            zf.setpassword(_PHRASE.encode())
            names = set(zf.namelist())
            assert "agents/A/files/small.txt" in names
            assert "agents/A/files/big.bin" not in names  # 超限被略過
            manifest = json.loads(zf.read("manifest.json"))
            assert any(s.get("reason") == "file_too_large" for s in manifest.get("skipped", []))
    finally:
        enc.unlink(missing_ok=True)


async def test_export_facts_unavailable_is_graceful(zone_data_root, monkeypatch):
    user = User(id="U2", email="u2@touka.io", hashed_password="h", owned_agents=["A"])
    monkeypatch.setattr(gx.session_service, "get_all_sessions", _empty)
    monkeypatch.setattr(gx.session_service, "get_all_projects", _empty)
    monkeypatch.setattr(gx, "find_avatar", lambda _uid: None)

    async def no_facts(_identity):
        return None  # 模擬記憶系統未連結

    enc = await gx.create_encrypted_export(user, _PHRASE, facts_fn=no_facts)
    try:
        with pyzipper.AESZipFile(str(enc)) as zf:
            zf.setpassword(_PHRASE.encode())
            names = set(zf.namelist())
            assert "agents/A/facts.json" not in names  # 無事實時不產生該檔
            assert "profile.json" in names  # 其餘照常匯出
            manifest = json.loads(zf.read("manifest.json"))
            assert manifest["counts"]["facts"] == 0
    finally:
        enc.unlink(missing_ok=True)


async def test_export_includes_the_activity_timeline_of_owned_agents(zone_data_root, monkeypatch):
    """活動時間軸（只有時間與識別碼）跟著自有 agent 匯出；別人的不在包裡。"""
    user = User(id="U3", email="u3@touka.io", hashed_password="h", owned_agents=["A"])
    monkeypatch.setattr(gx.session_service, "get_all_sessions", _empty)
    monkeypatch.setattr(gx.session_service, "get_all_projects", _empty)
    monkeypatch.setattr(gx, "find_avatar", lambda _uid: None)
    seen: list[str] = []

    async def fake_timeline(identity):
        seen.append(identity.agent_id)
        return [{"event_id": "user_turn:A:m1", "kind": "user_turn", "occurred_at": 5,
                 "session_id": "s1", "subject": "m1", "trust": "system", "metadata": {}}]

    monkeypatch.setattr(gx, "list_agent_events", fake_timeline)

    async def no_facts(_identity):
        return None

    enc = await gx.create_encrypted_export(user, _PHRASE, facts_fn=no_facts)
    try:
        with pyzipper.AESZipFile(str(enc)) as zf:
            zf.setpassword(_PHRASE.encode())
            timeline = json.loads(zf.read("agents/A/activity_timeline.json"))
            assert timeline["agent_id"] == "A" and timeline["count"] == 1
            assert timeline["events"][0]["event_id"] == "user_turn:A:m1"
            manifest = json.loads(zf.read("manifest.json"))
            assert manifest["counts"]["activity_events"] == 1
            assert "agent_activity_timeline" in manifest["included"]
            assert "activity_timeline.json" in zf.read("README.txt").decode()
    finally:
        enc.unlink(missing_ok=True)
    assert seen == ["A"]  # 只查自有 agent


async def test_export_lists_an_unreadable_timeline_in_skipped(zone_data_root, monkeypatch):
    user = User(id="U4", email="u4@touka.io", hashed_password="h", owned_agents=["A"])
    monkeypatch.setattr(gx.session_service, "get_all_sessions", _empty)
    monkeypatch.setattr(gx.session_service, "get_all_projects", _empty)
    monkeypatch.setattr(gx, "find_avatar", lambda _uid: None)

    async def broken_timeline(_identity):
        raise RuntimeError("synthetic timeline failure")

    monkeypatch.setattr(gx, "list_agent_events", broken_timeline)

    async def no_facts(_identity):
        return None

    enc = await gx.create_encrypted_export(user, _PHRASE, facts_fn=no_facts)
    try:
        with pyzipper.AESZipFile(str(enc)) as zf:
            zf.setpassword(_PHRASE.encode())
            assert "agents/A/activity_timeline.json" not in set(zf.namelist())
            manifest = json.loads(zf.read("manifest.json"))
            assert {"agent": "A", "reason": "activity_timeline_unavailable"} in manifest["skipped"]
            assert manifest["counts"]["activity_events"] == 0
    finally:
        enc.unlink(missing_ok=True)


# ── endpoint: 需登入 + 密碼閘 + 密語驗證 + happy path ───────────────────────────

def test_export_my_data_requires_auth():
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.post("/auth/export-my-data",
                  json={"current_password": "x", "passphrase": _PHRASE})
    assert resp.status_code in (401, 403), f"got {resp.status_code}"


def test_export_my_data_wrong_password():
    user = AuthService().create_user("gdpr_wrong@touka.io", "correct_pw_123", role="normal")
    app.dependency_overrides[get_current_user] = lambda: user
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.post("/auth/export-my-data",
                  json={"current_password": "WRONG", "passphrase": _PHRASE})
    _clear()
    assert resp.status_code == 403, f"got {resp.status_code}"  # 401 會被前端續期重送
    assert resp.json()["detail"]["code"] == "AUTH_REAUTH_FAILED"


def test_export_my_data_short_passphrase_422():
    user = AuthService().create_user("gdpr_short@touka.io", "correct_pw_123", role="normal")
    app.dependency_overrides[get_current_user] = lambda: user
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.post("/auth/export-my-data",
                  json={"current_password": "correct_pw_123", "passphrase": "short"})
    _clear()
    assert resp.status_code == 422, f"got {resp.status_code}"


def test_export_my_data_happy_path(monkeypatch, tmp_path):
    user = AuthService().create_user("gdpr_ok@touka.io", "correct_pw_123", role="normal")
    enc = tmp_path / "out.zip"
    enc.write_bytes(b"PK\x05\x06" + b"\x00" * 18)  # 最小空 ZIP（EOCD）

    async def fake_export(u, pw, **_kw):
        assert u.id == user.id
        assert pw == _PHRASE
        return enc

    monkeypatch.setattr("src.routers.auth.create_encrypted_export", fake_export)
    app.dependency_overrides[get_current_user] = lambda: user
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.post("/auth/export-my-data",
                  json={"current_password": "correct_pw_123", "passphrase": _PHRASE})
    _clear()
    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text[:200]}"
    assert resp.headers["content-type"] == "application/zip"
    assert "touka_export" in resp.headers.get("content-disposition", "")
