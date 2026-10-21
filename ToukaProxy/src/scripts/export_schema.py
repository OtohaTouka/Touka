"""導出 OpenAPI Schema 腳本 (OpenAPI Schema Exporter).

用於從 FastAPI 應用程式實例提取並寫入 openapi.json 結構描述檔，作為前後端型別同步的 SSOT 來源。
"""

import json
import sys
from pathlib import Path

# 將專案根目錄與 Memory Server 加入 sys.path 以便匯入
root = Path(__file__).resolve().parents[2] # ToukaProxy
sys.path.append(str(root))
sys.path.append(str(root.parent / "ToukaMemory" / "src"))

from src.main import app


def export_schema(output_path="openapi.json"):
    """產生並寫入系統的 OpenAPI schema 到指定路徑.

    Args:
        output_path (str, optional): 輸出 JSON 檔的物理路徑。預設為 "openapi.json"。
    """

    print(f"Generating OpenAPI schema for {app.title} (v{app.version})...")
    schema = app.openapi()
    
    # 確保輸出路徑正確
    with Path(output_path).open( "w", encoding="utf-8") as f:
        json.dump(schema, f, indent=2, ensure_ascii=False)
    
    print(f"Exported to: {Path(output_path).resolve()}")

if __name__ == "__main__":
    # 預設導出到專案根目錄
    out = sys.argv[1] if len(sys.argv) > 1 else "openapi.json"
    export_schema(out)
