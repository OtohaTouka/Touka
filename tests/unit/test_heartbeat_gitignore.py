"""G11（守衛涵蓋缺口待辦）— heartbeat 執行期設定必須保持 gitignore（token 防洩）。

缺口背景（見 docs/architecture/proposals/guard-coverage-gap-backlog.md · G11）：
    ADR-0150：heartbeat 執行期設定產物含生成的 heartbeat_token 與使用者排程，必須留在
    版控之外——root .gitignore 的 `heartbeat-config/`（＋相容保留的舊單檔 `heartbeat-config.json`）、
    `ToukaHeartbeat/.gitignore` 的 `config/`（＋活體 `config.json`）。repo 有開源計畫，
    誤入版控＝token 洩漏。

    此不變式**先前零機器執法**：唯一讀 .gitignore 的測試守的是「.dockerignore 不得被
    gitignore」此不同不變式；移除任一忽略行不會使任何測試失敗。本檔補上存在性守衛。
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _ignore_entries(path: Path) -> set[str]:
    """讀取 .gitignore 的有效條目（去註解／空行／前後空白）。"""
    if not path.is_file():
        return set()
    entries: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            entries.add(line)
    return entries


@pytest.mark.parametrize(
    ("gitignore_rel", "required"),
    [
        (".gitignore", "heartbeat-config/"),        # 目錄化後的活體設定目錄（ADR-0150）
        (".gitignore", "heartbeat-config.json"),    # 相容保留的舊單檔
        ("ToukaHeartbeat/.gitignore", "config/"),   # 開發版 compose 掛載的活體排程設定目錄
        ("ToukaHeartbeat/.gitignore", "config.json"),
    ],
)
def test_heartbeat_runtime_config_stays_gitignored(gitignore_rel: str, required: str):
    """heartbeat 執行期設定（含 heartbeat_token）的 gitignore 條目不得被移除。"""
    entries = _ignore_entries(ROOT / gitignore_rel)
    assert required in entries, (
        f"{gitignore_rel} 缺 `{required}` 忽略條目——heartbeat 執行期設定（含生成 token）"
        "可能被靜默提交入版控＝token 洩漏（ADR-0150/G11）"
    )
