"""Grok 生圖模型之 MCPServer，提供 grok_imagine 生圖工具。"""

import logging
import sys
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv
from mcp.server import MCPServer
from src.config import GROK_API_KEY, TOOL_BASE_URL
from src.models.identity import AgentIdentity
from src.services.resource_manager import resource_manager

# 載入環境變數（支援整合與獨立模式）
load_dotenv()

# 設定日誌：重要！必須輸出至 stderr，否則會干擾 MCP STDOUT 通訊
logging.basicConfig(  # noqa: BASICCONFIG_BAN — MCP 伺服器必須將日誌導向 stderr 以保護 stdout 作為 JSON-RPC 通訊通道
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr
)
logger = logging.getLogger("grok_imagine_server")

# 初始化 MCPServer
mcp = MCPServer("Grok Imagine", version="0.1.0")

# 配置與目錄初始化
BASE_DATA_DIR = resource_manager.get_base_path()
GROK_URL = "https://api.x.ai/v1/images/generations"

@mcp.tool()
async def grok_imagine(prompt: str, identity: AgentIdentity, aspect_ratio: str = "1:1") -> str:
    """使用 Grok (xAI) 的生圖模型產生圖片。
    符合路徑主權隔離：圖片將存入該 Agent 的私有 Vault 中。
    
    Args:
        prompt: 圖片描述詞 (Prompt)。
        identity: 執行者的 AgentIdentity (由系統自動注入)。
        aspect_ratio: 圖片比例，可選: 1:1, 4:3, 3:4, 16:9, 9:16。
    """
    if not GROK_API_KEY:
        return "❌ 錯誤：找不到 GROK_API_KEY 環境變數。請在 .env 中設定。"

    # --- SSOT: 透過 ResourceManager 取得隔離的輸出目錄 ---
    agent_id = identity.agent_id
    try:
        sov_output_dir = resource_manager.get_outputs_path(identity)
        if not sov_output_dir:
            return f"❌ 錯誤：無法獲取 Agent '{agent_id}' 的主權存儲空間。"
        target_dir = Path(sov_output_dir)
    except Exception as e:
        logger.error(f"ResourceManager 載入失敗，生圖終止以保護路徑主權: {e}")
        return f"❌ 錯誤：系統路徑服務異常，無法安全儲存圖片。({e})"
    
    resource_manager.make_dir(target_dir)

    logger.info(f"🎨 [Agent: {agent_id}] Generating image with prompt: {prompt}")

    headers = {
        "Authorization": f"Bearer {GROK_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": "grok-imagine-image",
        "prompt": prompt,
        "n": 1,
        "aspect_ratio": aspect_ratio
    }

    try:
        async with httpx.AsyncClient(timeout=90.0, follow_redirects=False) as client:  # 禁令 22
            resp = await client.post(GROK_URL, headers=headers, json=payload)
            
            if resp.status_code != 200:
                return f"❌ Grok API 錯誤 ({resp.status_code}): {resp.text}"
                
            data = resp.json()
            image_url = data.get("data", [{}])[0].get("url")
            if not image_url:
                return "❌ 錯誤：API 回傳成功但找不到圖片連結。"

            # 下載圖片
            filename = f"grok_{uuid.uuid4().hex[:8]}.png"
            filepath = target_dir / filename
            
            logger.info(f"💾 Downloading to sovereign path: {filepath}")
            image_resp = await client.get(image_url)
            image_resp.raise_for_status()
            
            resource_manager.write_file_binary(filepath, image_resp.content)
            
            # --- SSOT: 回傳符合主權隔離路由的 URL ---
            # 取得基礎 URL (優先讀取配置中心，否則預設 localhost:8000)
            base_url = TOOL_BASE_URL or "http://localhost:8000"
            sovereign_url = f"{base_url}/v1/outputs/{agent_id}/{filename}"
            
            return f"✅ 圖片生成成功！\n\n![{prompt}]({sovereign_url})\n\n*(原始描述: {prompt} | 儲存目錄: {agent_id}/outputs)*"

    except Exception as e:
        logger.error(f"🔥 Image generation failed: {e}")
        return f"❌ 生圖過程發生異常：{str(e)}"

if __name__ == "__main__":
    # 啟動 MCP 伺服器
    mcp.run()
