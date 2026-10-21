# ruff: noqa: T201, E402
"""ADR-0099 — Provider Parity Guard（第 36 道守衛）。

杜絕「後端 Provider Registry 與前端供應商金鑰清單漂移」（原 #7/#8 裂縫）：

不變量：**後端 registry 每個可發現（金鑰驅動）供應商的主要金鑰 env 名，都必須出現在前端
`ToukaChat/src/lib/providerKeys.ts` 的 env 清單中。** 後端新增一家供應商（如 DeepSeek）時，
若忘了在前端金鑰輸入 UI 加上對應項，本守衛即 fail-closed（CI 紅燈），避免「能路由/發現卻
無處輸入金鑰」。

方向為單向（backend ⊆ frontend）：前端可有額外非模型金鑰（如 Tavily 搜尋），不誤報。
以 registry 自身 API 為事實來源（非 regex 解析其原始碼），前端則純讀檔正規表達式擷取，
無網路、秒級、決定性。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for _p in (
    PROJECT_ROOT / "ToukaProxy",
    PROJECT_ROOT / "ToukaMemory" / "src",
    PROJECT_ROOT / "tests" / "smoke" / "lib",
):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from sandbox_env import enable_smoke_sandbox

if __name__ == "__main__":
    enable_smoke_sandbox("provider-parity-guard")

from src.services.providers import registry

PROVIDER_KEYS_TS = PROJECT_ROOT / "ToukaChat" / "src" / "lib" / "providerKeys.ts"
_ENV_RE = re.compile(r"""env:\s*['"]([A-Z0-9_]+)['"]""")


def frontend_env_names(ts_text: str) -> set[str]:
    """從 providerKeys.ts 擷取所有 `env: "X_API_KEY"` 鍵名。"""
    return set(_ENV_RE.findall(ts_text))


def backend_required_key_names() -> set[str]:
    """後端可發現供應商的主要金鑰 env 名（grok 取第一個 GROK_API_KEY）。"""
    return {
        spec.key_attrs[0]
        for spec in registry.discoverable_specs()
        if spec.key_attrs
    }


def run_checks() -> list[str]:
    """回傳所有違規訊息（空＝通過）。"""
    if not PROVIDER_KEYS_TS.exists():
        return [f"找不到前端供應商金鑰清單：{PROVIDER_KEYS_TS}"]
    fe = frontend_env_names(PROVIDER_KEYS_TS.read_text(encoding="utf-8"))
    required = backend_required_key_names()
    missing = sorted(required - fe)
    if missing:
        return [
            "後端 registry 有以下供應商金鑰，但前端 providerKeys.ts 缺對應 env："
            f"{missing}（請在 ToukaChat/src/lib/providerKeys.ts 補上 PROVIDER_KEYS 條目）",
        ]
    return []


def main() -> int:
    """執行 parity 檢查，印出結果並回傳 exit code（0 通過、1 失敗）。"""
    if sys.stdout.encoding != "utf-8" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 60)
    print("Provider Parity Guard (Guard #36)")
    print("=" * 60)
    errors = run_checks()
    if not errors:
        print("[OK] 後端 registry 供應商金鑰皆有前端 providerKeys.ts 對應項。")
        return 0
    print(f"[FAIL] 發現 {len(errors)} 項違規：")
    for e in errors:
        print("  " + e)
    return 1


if __name__ == "__main__":
    sys.exit(main())
