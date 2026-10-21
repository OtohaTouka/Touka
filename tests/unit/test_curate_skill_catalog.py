"""ADR-0074：策展工具 curate_skill_catalog 純函式 + 抓取流程測試（dev-time 工具）。"""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

# 工具位於 scripts/（非套件），以 importlib 載入（鏡像 test_guard_* 慣例）。
_TOOL = Path(__file__).resolve().parents[2] / "scripts" / "curate_skill_catalog.py"
_spec = importlib.util.spec_from_file_location("curate_skill_catalog", _TOOL)
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)


def test_raw_url():
    url = cc.raw_url("anthropics/skills", "a" * 40, "skills/mcp-builder", "SKILL.md")
    assert url == (
        "https://raw.githubusercontent.com/anthropics/skills/"
        + "a" * 40
        + "/skills/mcp-builder/SKILL.md"
    )


def test_detect_license_apache():
    assert cc.detect_license("Apache License\nVersion 2.0, January 2004") == "Apache-2.0"


def test_detect_license_mit():
    assert cc.detect_license("MIT License\n\nPermission is hereby granted, free of charge") == "MIT"


def test_detect_license_unknown():
    assert cc.detect_license("Some proprietary license, all rights reserved.") == "Unknown"


def test_list_files_filters_relativizes_sorts():
    tree = [
        {"type": "blob", "path": "skills/mcp-builder/scripts/x.py"},
        {"type": "blob", "path": "skills/mcp-builder/SKILL.md"},
        {"type": "tree", "path": "skills/mcp-builder/scripts"},
        {"type": "blob", "path": "skills/other/Y.md"},
    ]
    assert cc.list_files(tree, "skills/mcp-builder") == ["SKILL.md", "scripts/x.py"]


def test_build_entry_shape():
    files = [{"path": "SKILL.md", "sha256": "a" * 64}]
    e = cc.build_entry(
        skill_id="x", repo="r", commit="c", base_path="b", files=files, license_="MIT"
    )
    assert e["id"] == "x"
    assert e["files"] == files
    assert e["license"] == "MIT"


def test_file_sha256():
    assert cc.file_sha256(b"hello") == hashlib.sha256(b"hello").hexdigest()


def test_curate_end_to_end_mocked():
    def _get(url, *_a, **_k):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.status_code = 200
        if url.endswith("/commits/main"):
            resp.json.return_value = {"sha": "b" * 40}
        elif "git/trees" in url:
            resp.json.return_value = {
                "tree": [
                    {"type": "blob", "path": "skills/demo/SKILL.md"},
                    {"type": "blob", "path": "skills/demo/LICENSE.txt"},
                ],
                "truncated": False,
            }
        else:  # raw file fetch（content 供 sha256、text 供 LICENSE 偵測）
            resp.content = b"file-bytes"
            resp.text = "Apache License\nVersion 2.0"
        return resp

    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.get = MagicMock(side_effect=_get)

    with patch.object(cc.httpx, "Client", return_value=client):
        entry = cc.curate("owner/skills", "skills/demo")

    assert entry["id"] == "demo"
    assert entry["commit"] == "b" * 40
    assert [f["path"] for f in entry["files"]] == ["LICENSE.txt", "SKILL.md"]
    expected = hashlib.sha256(b"file-bytes").hexdigest()
    assert all(f["sha256"] == expected for f in entry["files"])
    assert entry["license"] == "Apache-2.0"
