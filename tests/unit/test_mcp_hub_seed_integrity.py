"""mcp_hub.json.example 種子誠信驗證（curated first-party ＋ 官方出廠貨架）。

`.example` 為 tracked 種子（bootstrap 自癒植入、fresh deploy 基線）。curated 條目
須誠實標示（非 Anthropic 官方）、命令安全、分類為前端功能性 taxonomy；官方出廠
條目（ADR-0189，F5）須與 ``official_hub_entry`` 配方 SSOT **逐欄精確相等**——
種子是配方的快照、不得自行漂移（ADR-0186 啟動規格改了，本測試立刻紅）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from src.utils.mcp_category import MCP_HUB_CATEGORIES
from src.utils.mcp_official_map import official_hub_entry
from src.utils.mcp_remote import validate_remote_url
from src.utils.mcp_security import is_command_safe

_EXAMPLE = (
    Path(__file__).resolve().parents[2] / "ToukaProxy" / "config" / "mcp_hub.json.example"
)

# 釘版標記（ADR-0191）：npm 走 ``pkg@1.2.3``（含 scope 形如 ``@scope/pkg@1.2.3``）、
# PyPI 走 PEP 440 的 ``pkg==1.2.3``。版本段須以數字起頭，擋掉 ``pkg@latest``／``pkg@next``
# 這類「看似釘版、實則浮動」的偽釘（本測試的真正防守對象）。
_NPM_PINNED_RE = re.compile(r"^(@[a-z0-9._-]+/)?[a-z0-9._-]+@\d[\w.\-+]*$", re.IGNORECASE)
_PYPI_PINNED_RE = re.compile(r"^[a-z0-9._-]+==\d[\w.\-+]*$", re.IGNORECASE)


def _hub() -> list[dict]:
    return json.loads(_EXAMPLE.read_text(encoding="utf-8"))["hub"]


def _package_arg(entry: dict) -> str:
    """取條目 args 中的「套件名位」（npx 為 ``-y`` 之後、uvx 為首位）。"""
    args = entry["args"]
    if entry["command"] == "npx":
        assert args[0] == "-y", f"{entry['id']}：npx 條目慣例首引數為 -y"
        return args[1]
    return args[0]


def test_example_exists_and_is_hub():
    """mcp_hub.json.example 為 tracked 種子，須存在且 hub 為 list。"""
    assert _EXAMPLE.exists(), "mcp_hub.json.example 應 tracked 於 repo"
    data = json.loads(_EXAMPLE.read_text(encoding="utf-8"))
    assert isinstance(data.get("hub"), list)


def test_curated_entries_honest_and_safe():
    """curated 條目：誠實標示（非官方）、命令過注入防護、分類合法。

    自 ADR-0194 起種子含 remote 條目（url／無 command），故安全檢查依形狀分寫——
    stdio 驗命令注入、remote 驗 URL 安全閘（明文 http 外連＝憑證明文過網）。
    """
    curated = [s for s in _hub() if s.get("source") == "curated"]
    assert curated, "種子應含至少一筆 curated first-party 條目"
    for s in curated:
        assert s["is_official"] is False, s["id"]
        assert s["category"] in MCP_HUB_CATEGORIES, s["id"]
        if s.get("url"):
            validate_remote_url(s["url"])
            assert s.get("command") is None, s["id"]
        else:
            assert is_command_safe(s["command"]), s["id"]
            assert isinstance(s["args"], list)


def test_seed_ships_official_starters():
    """出廠貨架（ADR-0189）：實證過的官方三條（fetch/filesystem/git）＋curated 皆在種子。"""
    ids = {s["id"] for s in _hub()}
    assert {"curated-mcp-docgen", "official-fetch",
            "official-filesystem", "official-git"} <= ids


def test_seed_ships_tier_a_connectors():
    """Tier A 貨架（ADR-0191）：經 spawn 實證＋對抗式覆核的第三方服務條目在種子。

    Stripe 刻意不在此列（實為 stdio↔HTTP 橋接器、本質屬 remote，待批次 2 以 remote
    形態重評）。Sentry 於收錄非 OSI 授權條目後補上（ADR-0192）。
    """
    ids = {s["id"] for s in _hub()}
    assert {"curated-notion", "curated-atlassian", "curated-figma",
            "curated-context7", "curated-sentry"} <= ids


def test_seed_ships_remote_connectors():
    """remote 貨架（ADR-0194）：免認證的公開端點條目在種子，且形狀為 remote。

    這兩條是 Touka 第一批 remote 形態的出廠條目（批次 2 開通傳輸後才可能存在），
    刻意選免金鑰者——使用者裝完即可用，不必先去申請憑證。
    """
    by_id = {s["id"]: s for s in _hub()}
    for entry_id in ("curated-ms-learn", "curated-deepwiki"):
        entry = by_id[entry_id]
        assert entry["url"].startswith("https://"), entry_id
        assert entry.get("command") is None, entry_id
        assert entry["env_requirements"] == [], entry_id


def test_credentialed_remote_entries_keep_secrets_in_env():
    """帶憑證的 remote 條目：header 只放 ``${VAR}`` 模板，實值走 env（ADR-0192/0194）。

    若哪天有人把 token 直接寫進 headers，就繞過了 `_preserve_masked_env` 的遮罩回填與
    防清除保護——這條測試把該設計釘死在種子層。
    """
    credentialed = {"curated-github", "curated-zapier", "curated-stripe"}
    by_id = {s["id"]: s for s in _hub()}
    for entry_id in credentialed:
        entry = by_id[entry_id]
        assert entry["env_requirements"], entry_id
        for value in entry["headers"].values():
            for var in entry["env_requirements"]:
                if f"${{{var}}}" in value:
                    break
            else:
                msg = f"{entry_id} 的 header 值 {value!r} 未引用任何 env_requirements 變數"
                raise AssertionError(msg)


def test_stripe_entry_discloses_unbounded_write_risk():
    """Stripe 是唯一無法出廠鎖唯讀的條目（ADR-0194）——描述必須據實揭露。

    GitHub 有 `/readonly` 端點、Atlassian 有 `READ_ONLY_MODE`，Stripe 兩者皆無：它含
    通用寫入原語，安全邊界完全落在使用者的受限金鑰權限上。誤以為「裝上去就安全」在
    金流情境的代價是真實金錢，故此揭露不得被日後的文案整理抹掉。
    """
    entry = next(s for s in _hub() if s["id"] == "curated-stripe")
    assert "rk_" in entry["description"]
    assert "唯讀" in entry["description"]
    assert "STRIPE_RESTRICTED_KEY" in entry["env_requirements"]


def test_non_osi_entry_discloses_its_license():
    """非 OSI 授權條目必須在 description 明示授權（ADR-0192）。

    owner 核准收錄 Sentry（FSL-1.1-ALv2）＝對「種子只收 OSI 開源」的準則開了具名例外。
    例外的代價是**揭露義務**：使用者在安裝前就該看得到這條不是 OSI 開源，而不是裝完
    才在 node_modules 裡發現。此測試釘死該揭露不被日後的文案整理悄悄抹掉。
    """
    entry = next(s for s in _hub() if s["id"] == "curated-sentry")
    assert "FSL-1.1" in entry["description"]
    assert "非 OSI" in entry["description"]


def test_third_party_entries_do_not_claim_official():
    """誠實標示（禁令 54）：非 Anthropic 官方的條目不得標 ``is_official``。

    種子內的第三方條目有兩種來歷——服務廠商第一方發行（Notion／Context7）與社群維護
    （Atlassian／Figma）——但兩者對 Touka 而言都不是「Anthropic 官方」，`is_official`
    一律為 False；社群條目另須在 description 明講非該服務官方發行，避免誤導。
    """
    for entry in _hub():
        if entry.get("source") == "curated":
            assert entry["is_official"] is False, entry["id"]
    community = {"curated-atlassian": "非 Atlassian 官方", "curated-figma": "非 Figma 官方"}
    by_id = {s["id"]: s for s in _hub()}
    for entry_id, disclaimer in community.items():
        assert disclaimer in by_id[entry_id]["description"], entry_id


def test_atlassian_recipe_pins_toolset_and_read_only():
    """Atlassian 出廠配方必須收斂工具面（ADR-0191）。

    實測：不設 TOOLSETS ＝ 98 個工具、約 22.5k token 的 schema，足以淹沒模型的可用
    context；`TOOLSETS=default` 降到 35／9.5k，再加 `READ_ONLY_MODE=true` 為 16／4k
    （default 工具組本身仍含 jira_delete_issue 等破壞性動作，兩個旋鈕正交、須同時下）。
    出廠取最保守組合，使用者要寫入權限可自行於設定頁調整。
    """
    entry = next(s for s in _hub() if s["id"] == "curated-atlassian")
    assert entry["env"]["TOOLSETS"] == "default"
    assert entry["env"]["READ_ONLY_MODE"] == "true"


def test_figma_recipe_carries_stdio_flag():
    """Figma 條目必須帶 ``--stdio``（ADR-0191）。

    實測：不帶此旗標時伺服器起 HTTP 模式、stdio 握手永不回應＝連線逾時。此為
    位置敏感的必要引數，不是可省略的裝飾。
    """
    entry = next(s for s in _hub() if s["id"] == "curated-figma")
    assert "--stdio" in entry["args"]


def test_every_seed_entry_pins_its_package_version():
    """釘版棘輪（ADR-0191）：種子每一條的套件名位都必須釘定版本。

    把禁令 55「只裝人工已審、pinned 來源」的精神延伸到 hub 的 npm/pypi
    條目——未釘版＝安裝當下抓上游最新版，等同把供應鏈信任交給「未來某個尚未存在
    的版本」。``@latest``／``@next`` 這類浮動標記亦視為未釘版（版本段須數字起頭）。
    """
    for entry in _hub():
        if entry.get("url"):
            # remote 條目（ADR-0192）無本地套件，版本由服務端掌握＝無從釘起。
            continue
        command = entry["command"]
        assert command in ("npx", "uvx"), f"{entry['id']}：未知 runtime {command!r}"
        pkg = _package_arg(entry)
        pattern = _NPM_PINNED_RE if command == "npx" else _PYPI_PINNED_RE
        assert pattern.match(pkg), (
            f"{entry['id']} 的套件引數 {pkg!r} 未釘版"
            f"（npx 需 pkg@x.y.z、uvx 需 pkg==x.y.z）"
        )


def test_seeded_official_entries_match_ssot_recipe():
    """種子官方條目＝``official_hub_entry`` 配方快照，逐欄精確相等（零漂移釘）。"""
    officials = [s for s in _hub() if s.get("source") == "official"]
    assert officials, "種子應含官方出廠條目"
    for s in officials:
        dir_name = s["id"].removeprefix("official-")
        expected, known = official_hub_entry(dir_name)
        assert known is True, f"{s['id']} 不在啟動規格表（不得上種子）"
        assert s == expected, f"{s['id']} 與配方 SSOT 漂移"
