# ruff: noqa: E402, T201, BLE001
"""Smoke Test: Context Snapshot 真實組裝資料形狀（ADR-0133）。

驗證 build_snapshot（純函式）由組裝後的 system／tools／home 產出結構化快照——
Debug Center「Context」分頁的資料來源。以代表性 system 字串驗證：有序區塊、token
成本、快取分層（stable/volatile）、tools/home 區塊。免 app／pipeline，快而穩。

身分：DebugAgent。
"""
import sys
from pathlib import Path

from lib.sandbox_env import enable_smoke_sandbox

enable_smoke_sandbox("context_snapshot")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
for _p in (PROJECT_ROOT / "ToukaProxy", PROJECT_ROOT / "ToukaMemory" / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from src.services import context_snapshot_service as css
from src.services.prompt_blocks import ALL_BLOCK_TAGS

_SYSTEM = (
    "<persona>You are Touka.</persona>\n\n"
    "<conversation_summary>earlier chat</conversation_summary>\n"
    "<tool_availability>foo denied</tool_availability>\n"
    "<validation_protocol>validate after edits</validation_protocol>\n"
    "<mission_awareness>progress 2/5</mission_awareness>\n"
)


def main() -> None:
    print("[*] Context Snapshot — 真實組裝資料形狀（ADR-0133）")
    failures: list[str] = []

    # 防漂移：每個 block tag 都要有 metadata
    if not set(ALL_BLOCK_TAGS) <= set(css.BLOCK_META):
        missing = set(ALL_BLOCK_TAGS) - set(css.BLOCK_META)
        failures.append(f"BLOCK_META 缺 tag：{missing}")

    snap = css.build_snapshot(
        agent_id="DebugAgent",
        model="claude-opus-4-8",
        system_text=_SYSTEM,
        tools=[{"function": {"name": "write"}}, {"function": {"name": "read"}}],
        home_messages=[{"role": "user", "content": "hi"}],
    )

    if snap.get("agent_id") != "DebugAgent":
        failures.append(f"agent_id 不符：{snap.get('agent_id')!r}")

    blocks = snap.get("blocks") or []
    names = [b.get("block_name") for b in blocks]
    tags = [b.get("tag") for b in blocks if b.get("tag")]

    # 有序：persona 在最前、被困的 tool_availability 在 summary/mission 之後被解析出
    if not names or not str(names[0]).startswith("Persona"):
        failures.append(f"首塊非 Persona：{names[:1]}")
    if "Tool Schemas" not in names or "Home Messages" not in names:
        failures.append("缺 Tool Schemas / Home Messages 區塊")

    # 快取分層（套真實 P4 函式）：本例穩定塊原被困在波動後 → 應重排
    if snap.get("cache_reordered") is not True:
        failures.append("cache_reordered 應為 True（穩定塊被困在波動後）")
    tier = {b["tag"]: b["cache_tier"] for b in blocks if b.get("tag") and b.get("present")}
    if tier.get("persona") != "stable" or tier.get("mission_awareness") != "volatile":
        failures.append(f"快取分層不符：{tier}")

    if not snap.get("total_tokens"):
        failures.append("total_tokens 為 0")

    print(f"    blocks={len(blocks)} tags={tags} tokens={snap.get('total_tokens')} "
          f"reordered={snap.get('cache_reordered')}")

    print("=" * 50)
    if failures:
        print(f"[FAIL] {len(failures)} 個問題：")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("[SUCCESS] Context Snapshot 真實組裝資料形狀正確。")
    sys.exit(0)


if __name__ == "__main__":
    if sys.platform == "win32":
        import io

        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    main()
