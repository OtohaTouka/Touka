# ruff: noqa: T201, E402
"""維護腳本：以目前 embedding 模型重建指定 agent 的向量記憶（預設重建 vault 內所有有記憶的 agent）.

只重建向量索引（從無損 FactualStore 讀事實重算），**不改事實**。
不升級記憶庫結構：版本落後就拒絕執行（請先啟動 Touka 完成升級）。
請於系統離線時執行。vault 路徑由參數提供（避免硬編碼），例如：

    python ToukaMemory/scripts/reembed_memory.py "D:/ToukaSanctuary/data/vault"

可加 ``--model`` / ``--agents`` 覆寫。
"""
import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (_ROOT / "ToukaProxy", _ROOT / "ToukaMemory" / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from memory_server.factual import FactsSchemaNotReadyError, FactsSchemaStatus
from memory_server.services.reembed import open_factual_store_for_reembed, reembed_agent
from memory_server.vector import VectorStore
from src.models.identity import AgentIdentity


def main() -> None:
    """解析參數並對指定 agent 逐一重建向量記憶（從無損 FactualStore 讀事實重算）。"""
    parser = argparse.ArgumentParser(description="Re-embed agent vector memory from FactualStore.")
    parser.add_argument("vault", help="Vault dir containing core_memory.db + _qdrant_db")
    parser.add_argument("--model", default="intfloat/multilingual-e5-base")
    parser.add_argument("--agents", nargs="+", default=None)
    args = parser.parse_args()

    print(f"[*] model = {args.model}")
    print(f"[*] vault = {args.vault}")
    try:
        factual = open_factual_store_for_reembed(args.vault)
    except FactsSchemaNotReadyError as exc:
        if exc.status is FactsSchemaStatus.OUTDATED:
            print("[ERROR] 記憶庫版本比這個工具舊：請先啟動 Touka 完成升級，再執行本工具。")
        else:
            print(f"[ERROR] 記憶庫目前無法使用（{exc.status.value}）：請看 Touka 日誌的")
            print("        [Factual] 那幾行。")
        sys.exit(2)
    vector_store = VectorStore(base_dir=args.vault, model_name=args.model)

    agents = args.agents
    if agents is None:
        # SSOT：未指定時，動態列舉該 vault FactualStore 內實際有記憶的 agent（不寫死 id）
        rows = factual.query_all_facts(include_archived=True, limit=1_000_000)
        agents = sorted({r["agent_id"] for r in rows if r.get("agent_id")})
        print(f"[*] agents = {agents}  (auto: all agents with memory in this vault)")
    else:
        print(f"[*] agents = {agents}")

    for agent_id in agents:
        identity = AgentIdentity(agent_id=agent_id, agent_role="Owner")
        count = reembed_agent(factual, vector_store, identity)
        print(f"[OK] {agent_id}: re-embedded {count} vectors")
    print("[DONE] 事實的索引狀態沒有改；啟動 Touka 後用 memory_tools 的 reindex 核對並標成 ready。")


if __name__ == "__main__":
    if sys.platform == "win32":
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    main()
