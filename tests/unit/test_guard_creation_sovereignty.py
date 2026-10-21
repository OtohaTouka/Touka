"""ADR-0069 Phase D — Creation Sovereignty Guard（#29）自我測試。

以 importlib 載入守衛、餵 in-memory 原始碼給純掃描函式；並驗證現況 repo 全綠。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_GUARD = Path(__file__).resolve().parents[2] / "scripts" / "guard_creation_sovereignty.py"


def _load():
    spec = importlib.util.spec_from_file_location("guard_creation_sov_under_test", _GUARD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # @dataclass 需能由 sys.modules 解析 __module__（py3.12）
    spec.loader.exec_module(mod)
    return mod


def test_route_gating_flags_ungated_endpoint():
    g = _load()
    src = (
        "from fastapi import APIRouter\n"
        "router = APIRouter(prefix='/creation')\n"
        "@router.post('/skill')\n"
        "async def ep(request):\n"
        "    return 1\n"
    )
    assert g._scan_route_gating(src)


def test_route_gating_clean_when_gated():
    g = _load()
    src = (
        "from fastapi import APIRouter, Depends\n"
        "router = APIRouter(prefix='/creation')\n"
        "@router.post('/skill')\n"
        "async def ep(request, _id=Depends(_require_creation_role)):\n"
        "    return 1\n"
    )
    assert g._scan_route_gating(src) == []


def test_engine_bypass_flags_privileged_calls():
    g = _load()
    assert g._scan_engine_bypass("svc.update_skill(x)\n")
    assert g._scan_engine_bypass("hub.register_server(a, b)\n")
    assert g._scan_engine_bypass("foo.bar()\n") == []


def test_facade_fastapi_flagged():
    g = _load()
    assert g._scan_facade_fastapi("from fastapi import APIRouter\n")
    assert g._scan_facade_fastapi("import fastapi\n")
    assert g._scan_facade_fastapi("import os\n") == []


def test_run_all_passes_on_current_repo():
    g = _load()
    assert g.run_all() == []
