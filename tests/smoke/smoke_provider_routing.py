# ruff: noqa: E402, T201, BLE001
"""Smoke Test: 外部供應商路由（Track A3）。

驗證 get_provider_info 依模型名關鍵字路由到正確供應商，未知供應商乾淨報錯
（ToukaException，不 crash）。對應「Touka 支援 Ollama/LiteLLM 等外部模型路由」。

註：OpenRouter 不靠關鍵字——它以自訂連線登錄，由 ModelManager 的不可變執行期路由
優先派往通用 OpenAI-相容分支，故不在本關鍵字測試內。

身分：N/A（純路由邏輯）。
"""
import sys
from pathlib import Path

from lib.sandbox_env import enable_smoke_sandbox

enable_smoke_sandbox("provider_routing")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
for _p in (PROJECT_ROOT / "ToukaProxy", PROJECT_ROOT / "ToukaMemory" / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from src.core.exceptions import ToukaException
from src.services.dispatchers.utils import get_provider_info

# 模型名關鍵字 → 期望供應商標籤
_CASES = [
    ("ollama-llama3.1", "Ollama"),
    ("litellm-proxy-model", "LiteLLM"),
    ("gpt-4o", "OpenAI"),
    ("openai-o3", "OpenAI"),
    ("grok-2", "Grok"),
    ("deepseek-chat", "DeepSeek"),
    ("deepseek-reasoner", "DeepSeek"),
]


def main() -> None:
    print("[*] Track A3 — 外部供應商路由 (get_provider_info)")
    failures: list[str] = []

    for name, expect in _CASES:
        try:
            url, _key, provider = get_provider_info(name)
            print(f"    {name!r} -> provider={provider!r} url={url!r}")
            if provider != expect:
                failures.append(f"{name!r} 路由到 {provider!r}，期望 {expect!r}")
        except Exception as e:
            failures.append(f"{name!r} 路由拋例外：{type(e).__name__}: {e}")

    # 未知供應商 → 乾淨報 ToukaException（MODEL_NOT_FOUND），不可裸 crash
    try:
        get_provider_info("totally-unknown-model-xyz")
        failures.append("未知模型未報錯（應 raise ToukaException）")
    except ToukaException:
        print("    未知模型 -> 正確 raise ToukaException ✔")
    except Exception as e:
        failures.append(f"未知模型報錯型別非 ToukaException：{type(e).__name__}")

    print("=" * 50)
    if failures:
        print(f"[FAIL] {len(failures)} 個問題：")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("[SUCCESS] 外部供應商關鍵字路由正常（Ollama/LiteLLM/OpenAI/Grok ＋未知乾淨報錯）。")
    sys.exit(0)


if __name__ == "__main__":
    if sys.platform == "win32":
        import io

        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    main()
