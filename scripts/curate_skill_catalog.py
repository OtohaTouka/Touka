# ruff: noqa: T201
"""ADR-0074 dev-time 策展工具：產出 skill_catalog.json.example 的 catalog entry。

給定 ``<owner/repo> <base_path>``，用 GitHub API 解析**釘版 commit-SHA**、遞迴列出
base_path 下**確切 files**，並偵測該 skill 的 LICENSE，輸出 catalog entry JSON 供人審
（name/description/author 由人依 SKILL.md 補實，並務必人工讀過 SKILL.md 與 scripts）。

**dev-time only**：本工具用 ``api.github.com``；**嚴禁** import 進 ``ToukaProxy/src``
（runtime 安裝只走 ``raw.githubusercontent.com``、零 api.github.com — 見 ADR-0074）。

用法::

    python scripts/curate_skill_catalog.py anthropics/skills skills/mcp-builder [ref]
"""
from __future__ import annotations

import hashlib
import json
import sys

import httpx

_API = "https://api.github.com"
_RAW = "https://raw.githubusercontent.com"
ALLOWED_LICENSES = ("Apache-2.0", "MIT", "BSD-3-Clause")
_LICENSE_NAMES = ("LICENSE.txt", "LICENSE", "LICENSE.md")


def raw_url(repo: str, commit: str, base_path: str, rel: str) -> str:
    """組裝 runtime 安裝會用的釘版 raw URL（與 install_skill_from_catalog 一致）。"""
    return f"{_RAW}/{repo}/{commit}/{base_path}/{rel}"


def file_sha256(content: bytes) -> str:
    """檔案內容的 sha256（64-hex）；與安裝期逐檔內容驗證一致。"""
    return hashlib.sha256(content).hexdigest()


def detect_license(text: str) -> str:
    """從 LICENSE 內容粗略偵測授權；僅認開源許可，否則回 'Unknown'（人工複核）。"""
    head = text[:600]
    if "Apache License" in head:
        return "Apache-2.0"
    if "MIT License" in head or "Permission is hereby granted, free of charge" in head:
        return "MIT"
    if "Redistribution and use" in head and "BSD" in text[:1500]:
        return "BSD-3-Clause"
    return "Unknown"


def list_files(tree: list[dict], base_path: str) -> list[str]:
    """從 git/trees 回應挑出 base_path 下的 blob，回傳相對 base_path 的路徑（已排序）。"""
    prefix = base_path.rstrip("/") + "/"
    return sorted(
        item["path"][len(prefix):]
        for item in tree
        if item.get("type") == "blob" and item.get("path", "").startswith(prefix)
    )


def build_entry(
    *,
    skill_id: str,
    repo: str,
    commit: str,
    base_path: str,
    files: list[dict],
    license_: str,
    name: str = "",
    category: str = "Dev",
    description: str = "",
    author: str = "",
) -> dict:
    """組裝一筆 catalog entry（純函式；name/description/author 由人補實）。"""
    return {
        "id": skill_id,
        "name": name or skill_id,
        "category": category,
        "description": description,
        "repo": repo,
        "commit": commit,
        "base_path": base_path,
        "files": files,
        "author": author,
        "license": license_,
    }


def curate(repo: str, base_path: str, ref: str = "main") -> dict:
    """抓取並組裝一筆 entry（網路：commit 釘版、逐檔 sha256、license 偵測）。"""
    with httpx.Client(timeout=30.0, follow_redirects=False) as client:
        head = client.get(f"{_API}/repos/{repo}/commits/{ref}")
        head.raise_for_status()
        commit = head.json()["sha"]

        tree_res = client.get(f"{_API}/repos/{repo}/git/trees/{commit}?recursive=1")
        tree_res.raise_for_status()
        payload = tree_res.json()
        if payload.get("truncated"):
            print("⚠️ git tree 被截斷（repo 太大）— files 清單可能不完整，請改用子樹查詢。")
        paths = list_files(payload.get("tree", []), base_path)

        # 逐檔抓 raw 內容算 sha256（與安裝期驗證對齊）；順帶從 LICENSE 內容偵測授權。
        files: list[dict] = []
        license_ = "Unknown"
        for rel in paths:
            blob = client.get(raw_url(repo, commit, base_path, rel))
            blob.raise_for_status()
            files.append({"path": rel, "sha256": file_sha256(blob.content)})
            if rel in _LICENSE_NAMES:
                license_ = detect_license(blob.text)

    return build_entry(
        skill_id=base_path.rstrip("/").split("/")[-1],
        repo=repo,
        commit=commit,
        base_path=base_path,
        files=files,
        license_=license_,
    )


def main(argv: list[str]) -> int:
    """CLI：產出一筆 catalog entry JSON 供人審（name/description/author 待補）。"""
    if len(argv) < 2:
        print("用法：python scripts/curate_skill_catalog.py <owner/repo> <base_path> [ref]")
        return 2
    repo, base_path = argv[0], argv[1]
    ref = argv[2] if len(argv) > 2 else "main"
    entry = curate(repo, base_path, ref)
    if entry["license"] not in ALLOWED_LICENSES:
        print(f"⚠️ 授權偵測為 '{entry['license']}'（非開源許可集）— 請人工確認後再收錄。")
    print(json.dumps(entry, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
