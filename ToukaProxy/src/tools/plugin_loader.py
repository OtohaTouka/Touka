"""外掛與 MCP 動態載入引擎 (Plugin & MCP Dynamic Loader).

負責掃描、解析並掛載第三方技能擴展。
具備自動 Schema 生成功能，支援從 Python Type Hints 與 Docstrings 提取元數據 (Metadata)，
並同時相容 Touka Native 與 Open WebUI 外掛格式。
"""

import importlib.util
import inspect
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def get_type_name(annotation: Any) -> str:
    """將 Python 型別標註轉譯為 JSON Schema 基礎型別.

    Args:
        annotation (Any): Python 型別標註物件.

    Returns:
        str: 對應的 JSON 類型字串 ('string', 'integer', 'boolean', 'array').
    """
    if hasattr(annotation, "__name__"):
        name = annotation.__name__.lower()
        if name in ["str", "string"]:
            return "string"
        if name in ["int", "integer"]:
            return "integer"
        if name in ["bool", "boolean"]:
            return "boolean"
        if name in ["list", "tuple", "set"]:
            return "array"
    return "string"


def parse_docstring(docstring: str | None) -> tuple[str, dict[str, str]]:
    """解析函數 Docstring 以提取描述與參數說明.

    支援傳統 :param p_name: 格式解析，用於相容舊版外掛.

    Args:
        docstring (str | None): 原始註解字串.

    Returns:
        tuple[str, dict[str, str]]: 包含 (主體描述, 參數描述字典) 的元組.
    """
    if not docstring:
        return "無工具說明", {}

    lines = docstring.strip().split("\n")
    main_desc = []
    params = {}

    for line in lines:
        line = line.strip()
        if line.startswith(":param"):
            # 格式: :param p_name: 描述
            match = re.search(r":param\s+(\w+):(.*)", line)
            if match:
                p_name, p_desc = match.groups()
                params[p_name] = p_desc.strip()
        elif not line.startswith(":") and not params:
            main_desc.append(line)

    return " ".join(main_desc), params


def generate_schema_from_method(m_name: str, method: Callable[..., Any]) -> dict[str, Any]:
    """自動根據類別方法產生 OpenAI Function Call Schema (元編程引擎).

    透過 inspect 模組動態分析函數簽章 (Signature) 與註解.

    Args:
        m_name (str): 方法名稱.
        method (Callable): 實體方法物件.

    Returns:
        dict[str, Any]: 完整符合功能的 JSON Schema.
    """
    doc_desc, param_descriptions = parse_docstring(method.__doc__)
    sig = inspect.signature(method)

    properties = {}
    required = []

    for p_name, p_obj in sig.parameters.items():
        if p_name == "self":
            continue

        p_type = get_type_name(p_obj.annotation)
        properties[p_name] = {
            "type": p_type
            if p_type in ["string", "integer", "boolean", "array"]
            else "string",
            "description": param_descriptions.get(p_name, f"參數 {p_name}"),
        }

        if p_obj.default == inspect.Parameter.empty:
            required.append(p_name)

    return {
        "type": "function",
        "function": {
            "name": m_name,
            "description": doc_desc,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


async def load_plugins(
    plugin_dir: Path | str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Callable[..., Any]]]:
    """掃描並從指定目錄動態載入外掛模組.

    支援 Touka Native 模式 (SCHEMA + handler) 
    與 Open WebUI 模式 (Tools class with methods).

    Args:
        plugin_dir (Path | str | None): 外掛目錄路徑。預設會查找 PLUGIN_DIR 環境變數或 src/plugins.

    Returns:
        tuple: (Schema 列表, 註冊表字典).
    """
    if plugin_dir is None:
        from src.config import PLUGIN_DIR
        plugin_path = PLUGIN_DIR if PLUGIN_DIR else Path(__file__).parent.parent / "plugins"
    else:
        plugin_path = Path(plugin_dir)

    all_schemas = []
    all_registry = {}

    if not plugin_path.exists():
        logger.warning(f"Plugin directory not found: {plugin_path}")
        return [], {}

    # 遍歷目錄下的 Python 檔案
    for file in plugin_path.glob("*.py"):
        if file.name.startswith("__"):
            continue

        module_name = f"dynamic_plugin_{file.stem}"

        try:
            spec = importlib.util.spec_from_file_location(module_name, str(file))
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # --- 模式 A: Touka Native 格式 ---
            if hasattr(module, "SCHEMA") and hasattr(module, "handler"):
                all_schemas.append(module.SCHEMA)
                all_registry[module.SCHEMA["function"]["name"]] = module.handler
                logger.debug(f"Loaded Native Plugin: {module.SCHEMA['function']['name']}")

            # --- 模式 B: Open WebUI 格式 (類別導向) ---
            elif hasattr(module, "Tools"):
                instance = module.Tools()
                methods = inspect.getmembers(instance, predicate=inspect.ismethod)

                for m_name, m_obj in methods:
                    if m_name.startswith("_"):
                        continue

                    schema = generate_schema_from_method(m_name, m_obj)
                    all_schemas.append(schema)
                    all_registry[m_name] = m_obj
                    logger.debug(f"Loaded Open WebUI Plugin Skill: {m_name}")

        except Exception:
            logger.exception(f"🔥 Failed to load plugin from {file}")

    return all_schemas, all_registry
