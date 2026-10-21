"""Unit tests for guard_endpoint_auth.py (TDD).

Tests verify that the endpoint auth guard correctly:
- Detects endpoints missing auth dependencies
- Respects the intentionally-public allowlist
- Handles edge cases (custom auth guards, decorators)
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

# Import scripts following the same pattern as test_lint_ssot.py
_PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()
sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))

from guard_endpoint_auth import (  # noqa: E402
    ALLOWLIST,
    GRANDFATHER,
    ROUTERS_DIR,
    EndpointViolation,
    check_admin_level,
    collect_endpoints,
    scan_router_file,
)


def _write_router(tmp_path: Path, content: str) -> Path:
    """Write a temp router file and return its path."""
    f = tmp_path / "test_router.py"
    f.write_text(textwrap.dedent(content), encoding="utf-8")
    return f


def test_multiple_router_receivers_keep_their_own_prefix(tmp_path: Path) -> None:
    source = _write_router(
        tmp_path,
        """
        router = APIRouter(prefix="/heartbeat")
        other = APIRouter(prefix="/other")
        @router.get("/test")
        def public_probe(): pass
        @other.post("/protected")
        def missing_auth(): pass
    """,
    )
    records = collect_endpoints(source)
    assert {r.path for r in records} == {"/heartbeat/test", "/other/protected"}
    assert [r.path for r in scan_router_file(source)] == ["/other/protected"]


def test_nested_local_router_keeps_parent_prefix_and_auth_failure(tmp_path: Path) -> None:
    source = _write_router(
        tmp_path,
        """
        router = APIRouter(prefix="/heartbeat")
        schedules = APIRouter(prefix="/schedules")
        @schedules.post("")
        def missing_auth(): pass
        router.include_router(schedules, prefix="/managed")
    """,
    )
    assert [r.path for r in scan_router_file(source)] == ["/heartbeat/managed/schedules"]


def test_real_heartbeat_routes_have_exact_static_paths() -> None:
    records = collect_endpoints(ROUTERS_DIR / "heartbeat.py")
    paths = {r.function: r.path for r in records}
    assert {(r.method, r.path) for r in records if "human_heartbeat" in r.function} == {
        ("get", "/heartbeat/schedules"),
        ("post", "/heartbeat/schedules"),
        ("delete", "/heartbeat/schedules/{schedule_id:path}"),
    }
    assert paths["test_heartbeat_connectivity"] == "/heartbeat/test"
    assert paths["list_human_heartbeat_schedules"] == "/heartbeat/schedules"
    assert paths["delete_human_heartbeat_schedule"] == "/heartbeat/schedules/{schedule_id:path}"


@pytest.mark.parametrize(
    "source",
    [
        "router = APIRouter(prefix=dynamic_prefix)",
        'router = APIRouter()\n@unknown.get("/x")\ndef endpoint(): pass',
        (
            "router = APIRouter()\nchild = APIRouter()\n"
            "router.include_router(child)\nchild.include_router(router)"
        ),
        (
            "router = APIRouter()\nchild = APIRouter()\nother = APIRouter()\n"
            "router.include_router(child)\nother.include_router(child)"
        ),
    ],
)
def test_unresolved_router_registration_fails_loud(tmp_path: Path, source: str) -> None:
    with pytest.raises(ValueError, match="router"):
        collect_endpoints(_write_router(tmp_path, source))


# ---------------------------------------------------------------------------
# Basic detection
# ---------------------------------------------------------------------------


def test_detects_endpoint_without_any_depends(tmp_path):
    """An endpoint with no Depends at all must be flagged."""
    f = _write_router(
        tmp_path,
        """
        from fastapi import APIRouter
        router = APIRouter()

        @router.post("/secret")
        async def secret_action():
            return {}
    """,
    )
    violations = scan_router_file(f)
    assert len(violations) == 1
    assert violations[0].path == "/secret"
    assert violations[0].method == "post"


def test_detects_endpoint_with_only_non_auth_depends(tmp_path):
    """A Depends() that is NOT an auth guard must still be flagged."""
    f = _write_router(
        tmp_path,
        """
        from fastapi import APIRouter, Depends
        router = APIRouter(prefix="/ui")

        def get_db(): pass

        @router.delete("/item/{id}")
        async def delete_item(db=Depends(get_db)):
            return {}
    """,
    )
    violations = scan_router_file(f)
    assert any(v.path == "/ui/item/{id}" for v in violations)


# ---------------------------------------------------------------------------
# Auth guard recognition
# ---------------------------------------------------------------------------


def test_passes_endpoint_with_get_verified_identity(tmp_path):
    """Endpoint with Depends(get_verified_identity) must pass."""
    f = _write_router(
        tmp_path,
        """
        from fastapi import APIRouter, Depends
        from src.services.identity_router import get_verified_identity
        router = APIRouter()

        @router.get("/protected")
        async def protected(identity=Depends(get_verified_identity)):
            return {}
    """,
    )
    violations = scan_router_file(f)
    assert not violations, f"Unexpected violations: {violations}"


def test_passes_endpoint_with_custom_role_guard(tmp_path):
    """Endpoint with a custom _require_*_role Depends must pass."""
    f = _write_router(
        tmp_path,
        """
        from fastapi import APIRouter, Depends
        router = APIRouter()

        def _require_admin_role(): pass

        @router.post("/admin/action")
        async def admin_action(identity=Depends(_require_admin_role)):
            return {}
    """,
    )
    violations = scan_router_file(f)
    assert not violations


def test_passes_endpoint_with_require_admin(tmp_path):
    """Endpoint using require_admin must pass."""
    f = _write_router(
        tmp_path,
        """
        from fastapi import APIRouter, Depends
        router = APIRouter()

        def require_admin(): pass

        @router.get("/admin/users")
        async def list_users(a=Depends(require_admin)):
            return {}
    """,
    )
    violations = scan_router_file(f)
    assert not violations


def test_passes_endpoint_with_get_current_user(tmp_path):
    """Endpoint using get_current_user (auth.py pattern) must pass."""
    f = _write_router(
        tmp_path,
        """
        from fastapi import APIRouter, Depends
        router = APIRouter()

        def get_current_user(): pass

        @router.get("/me")
        async def get_me(user=Depends(get_current_user)):
            return {}
    """,
    )
    violations = scan_router_file(f)
    assert not violations


# ---------------------------------------------------------------------------
# Allowlist behaviour
# ---------------------------------------------------------------------------


def test_allowlist_is_non_empty():
    """The ALLOWLIST must contain entries for known public endpoints."""
    assert len(ALLOWLIST) > 0, "ALLOWLIST must not be empty"


def test_allowlist_entry_contains_required_fields():
    """Each allowlist entry must have 'method' and 'path_prefix' keys."""
    for entry in ALLOWLIST:
        assert "method" in entry, f"Entry missing 'method': {entry}"
        assert "path_prefix" in entry, f"Entry missing 'path_prefix': {entry}"


def test_health_endpoint_is_in_allowlist():
    """GET /health must be in the allowlist as an observability endpoint."""
    public = [(e["method"], e["path_prefix"]) for e in ALLOWLIST]
    assert ("get", "/health") in public or any(
        e["method"] == "get" and "/health" in e["path_prefix"] for e in ALLOWLIST
    ), "/health GET must be in the allowlist"


def test_auth_login_is_in_allowlist():
    """POST /auth/login must be in the allowlist as an auth flow entry point."""
    assert any(e["method"] == "post" and "login" in e["path_prefix"] for e in ALLOWLIST), (
        "/auth/login must be in the allowlist"
    )


# ---------------------------------------------------------------------------
# EndpointViolation model
# ---------------------------------------------------------------------------


def test_router_prefix_prepended_to_path(tmp_path):
    """Router prefix must be included in the violation path."""
    f = _write_router(
        tmp_path,
        """
        from fastapi import APIRouter
        router = APIRouter(prefix="/admin")

        @router.get("/users")
        async def list_users():
            return {}
    """,
    )
    violations = scan_router_file(f)
    assert violations
    assert violations[0].path == "/admin/users"


def test_allowlisted_endpoint_not_flagged(tmp_path):
    """Allowlisted endpoint without auth must pass."""
    f = _write_router(
        tmp_path,
        """
        from fastapi import APIRouter
        router = APIRouter()

        @router.get("/health")
        async def health():
            return {"status": "ok"}
    """,
    )
    violations = scan_router_file(f)
    assert not violations, f"Allowlisted /health should not be flagged: {violations}"


def test_endpoint_violation_str():
    """EndpointViolation must produce a readable string."""
    v = EndpointViolation(
        file=Path("routers/test.py"),
        function="my_func",
        method="post",
        path="/test",
        line=42,
    )
    s = str(v)
    assert "my_func" in s
    assert "/test" in s
    assert "42" in s


# ---------------------------------------------------------------------------
# E05：GRANDFATHER 永久空清單靜態斷言（ADR-0034）
# ---------------------------------------------------------------------------


def test_grandfather_list_is_empty():
    """技術債逃生口 GRANDFATHER 必須永久為空——加任何條目即靜默豁免端點認證。"""
    assert GRANDFATHER == [], (
        "GRANDFATHER 必須保持空清單（ADR-0034）；"
        f"不得以 grandfather 豁免端點，現有 {len(GRANDFATHER)} 條"
    )


# ---------------------------------------------------------------------------
# E06/E07：admin 等級雙向同步（家族④）
# ---------------------------------------------------------------------------

_ADMIN_ROUTER = """
    from fastapi import APIRouter, Depends
    router = APIRouter(prefix="/system")

    def _require_admin(): pass
    def get_verified_identity(): pass

    @router.post("/sliding_window")
    async def update_sw(a=Depends(_require_admin)):
        return {}

    @router.get("/status")
    async def status(a=Depends(get_verified_identity)):
        return {}
"""


def test_admin_level_in_sync_passes(tmp_path):
    """表列端點確實帶 admin 級 Depends → 零錯誤。"""
    records = collect_endpoints(_write_router(tmp_path, _ADMIN_ROUTER))
    registry = [{"method": "post", "path": "/system/sliding_window"}]
    assert check_admin_level(records, registry) == []


def test_admin_downgrade_detected(tmp_path):
    """require_admin → get_verified_identity 降級必被抓（E06/E07 核心）。"""
    downgraded = _ADMIN_ROUTER.replace(
        "async def update_sw(a=Depends(_require_admin))",
        "async def update_sw(a=Depends(get_verified_identity))",
    )
    records = collect_endpoints(_write_router(tmp_path, downgraded))
    registry = [{"method": "post", "path": "/system/sliding_window"}]
    errors = check_admin_level(records, registry)
    assert any("ADMIN DOWNGRADE" in e for e in errors)


def test_unregistered_admin_endpoint_detected(tmp_path):
    """帶 admin 級 Depends 但未列表 → 抓漂移（防清單靜默過期）。"""
    records = collect_endpoints(_write_router(tmp_path, _ADMIN_ROUTER))
    errors = check_admin_level(records, registry=[])  # 空表：sliding_window 未登記
    assert any("UNREGISTERED ADMIN" in e for e in errors)


def test_stale_registry_entry_detected(tmp_path):
    """表列端點已不存在（改名/刪除）→ 抓過期表。"""
    records = collect_endpoints(_write_router(tmp_path, _ADMIN_ROUTER))
    registry = [{"method": "post", "path": "/system/GONE"}]
    errors = check_admin_level(records, registry)
    assert any("REGISTRY STALE" in e for e in errors)


def test_real_admin_registry_in_sync():
    """生產樹：ADMIN_REQUIRED 與實際 admin 端點雙向一致（活體同步鎖）。"""
    records = []
    for f in sorted(ROUTERS_DIR.glob("*.py")):
        if f.name != "__init__.py":
            records.extend(collect_endpoints(f))
    assert check_admin_level(records) == []


@pytest.mark.parametrize(
    "source",
    [
        "router = APIRouter(**options)",
        ("router = APIRouter()\nchild = APIRouter()\nrouter.include_router(child, **options)"),
        "router = APIRouter(prefix='/real')\nrouter = replacement",
    ],
)
def test_expanded_or_reassigned_router_fails_loud(tmp_path: Path, source: str) -> None:
    with pytest.raises(ValueError, match="router"):
        collect_endpoints(_write_router(tmp_path, source))
