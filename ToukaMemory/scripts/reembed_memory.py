# ruff: noqa: T201, E402
"""維護腳本：以目前 embedding 模型重建指定 agent 的向量記憶（預設重建 vault 內所有有記憶的 agent）.

只重建向量索引（從無損 FactualStore 讀事實重算），**不改事實**。
不升級記憶庫結構：版本落後就拒絕執行（請先啟動 Touka 完成升級）。
請於系統離線時執行。vault 路徑由參數提供（避免硬編碼），例如：

    python ToukaMemory/scripts/reembed_memory.py "D:/ToukaSanctuary/data/vault"

沒給 ``--model`` 時用設定的嵌入模型（環境或設定檔的 ``EMBEDDING_MODEL``，都沒有用內建預設），
開跑先印出用哪個、從哪來；給了和設定不同的照跑、印警告（ADR-0315）。``--agents`` 可指定 agent。
Docker 裡用 ``docker compose exec`` 跑時讀的是容器建立時的環境；要用目前設定檔的值，請用
``docker compose run --rm``。
"""
import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (_ROOT / "ToukaProxy", _ROOT / "ToukaMemory" / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from memory_server.config_utils import get_embedding_model_and_source
from memory_server.factual import FactsSchemaNotReadyError, FactsSchemaStatus
from memory_server.services.reembed import open_factual_store_for_reembed, reembed_agent
from memory_server.utils.embedding_index import same_model
from memory_server.vector import VectorStore
from src.models.identity import AgentIdentity


def choose_model(cli_model: str | None) -> tuple[str, str, str | None]:
    """（要用的模型, 來源, 設定的模型——只有給了不同的 ``--model`` 時才有，用來印警告）."""
    configured, source = get_embedding_model_and_source()
    if not cli_model:
        return configured, source, None
    return cli_model, "--model", None if same_model(cli_model, configured) else configured


def main() -> None:
    """解析參數並對指定 agent 逐一重建向量記憶（從無損 FactualStore 讀事實重算）。"""
    parser = argparse.ArgumentParser(description="Re-embed agent vector memory from FactualStore.")
    parser.add_argument("vault", help="Vault dir containing core_memory.db + _qdrant_db")
    parser.add_argument("--model", default=None, help="Default: the configured EMBEDDING_MODEL")
    parser.add_argument("--agents", nargs="+", default=None)
    args = parser.parse_args()

    model, source, configured = choose_model(args.model)
    print(f"[*] model = {model}  (from: {source})")
    if configured is not None:
        print(f"[WARN] --model 和設定的嵌入模型（{configured}）不同：重建後 Touka 會提示這些記憶")
        print("       用的是別的模型、語意搜尋用不到，直到設定改成同一個模型。")
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
    vector_store = VectorStore(base_dir=args.vault, model_name=model)

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
