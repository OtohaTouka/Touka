"""實際驗證用的後端：在 smoke 沙盒裡跑真的 Proxy，模型只連本機的假模型（ADR-0295）.

由 ``ToukaChat/e2e-real/global-setup.ts`` 啟動；檔名不符合 smoke 的自動 discovery。

- 沙盒與外連封鎖由 ``enable_smoke_sandbox`` 負責，必須在匯入任何 ``src`` 之前。
- 標準輸入關閉就正常結束：atexit 清沙盒、把外連違規變成非 0 結束碼，所以測試要看結束碼。
"""

from __future__ import annotations

import argparse
import gc
import json
import logging
import os
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tests" / "smoke"))

from lib.sandbox_env import enable_smoke_sandbox  # noqa: E402

HOST = "127.0.0.1"
# 每個頁面載入都會換一次 refresh token，預設上限（15 分鐘 30 次）不夠跑完整套；只放寬這個沙盒。
AUTH_RATE_LIMIT_MAX_FOR_E2E = "1000"

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--api-port", type=int, required=True)
parser.add_argument("--fake-model-url", required=True)
parser.add_argument("--fake-model-api-key", required=True)
parser.add_argument("--web-origin", required=True)
parser.add_argument("--fake-mcp-port", type=int, required=True)
parser.add_argument("--heartbeat-token", required=True)
args = parser.parse_args()

sandbox_root = Path(enable_smoke_sandbox("e2e_real"))
os.environ.update(  # noqa: ENV_SSOT_VIOLATION - 沙盒建好之後、載入產品之前
    {
        # 沙盒預設關掉模型探索；假模型要靠它進選單，連線又只有本機假模型（ADR-0295）
        "ENABLE_DYNAMIC_MODELS": "true",
        "SECURE_COOKIES": "false",
        "UI_DEFAULT_URL": args.web_origin,
        "AUTH_RATE_LIMIT_MAX": AUTH_RATE_LIMIT_MAX_FOR_E2E,
        # 排程服務沒有跑，測試自己帶這個權杖打喚醒端點
        "HEARTBEAT_TOKEN": args.heartbeat_token,
    }
)
(sandbox_root / "config" / "connections.json").write_text(
    json.dumps(
        {
            "connections": [
                {
                    "id": "fake",
                    "name": "Fake",
                    "url": args.fake_model_url,
                    "apiKey": args.fake_model_api_key,
                    "provider": "custom",
                    "protocol": "openai",
                    "enabled": True,
                }
            ]
        }
    ),
    encoding="utf-8",
)
# 設定頁一打開就會讀精選 MCP 清單，清單不是空的就會去 npm 查下載量；沙盒裡不能連外。
(sandbox_root / "config" / "mcp_hub.json").write_text(json.dumps({"hub": []}), encoding="utf-8")
(sandbox_root / "config" / "server_config.json").write_text(
    json.dumps({"mcpServers": {"e2e": {"url": f"http://{HOST}:{args.fake_mcp_port}/mcp"}}}),
    encoding="utf-8",
)
sys.stdout.write(f"SANDBOX_ROOT={sandbox_root}\n")
sys.stdout.flush()

sys.path[:0] = [str(REPO_ROOT / "ToukaProxy"), str(REPO_ROOT / "ToukaMemory" / "src")]

import fake_mcp_server  # noqa: E402
import proactor_close_fix  # noqa: E402
import uvicorn  # noqa: E402
from src.config import get_app_data_dir, get_config_dir  # noqa: E402
from src.main import app  # noqa: E402

for resolved in (Path(get_app_data_dir()), get_config_dir()):
    if not resolved.resolve().is_relative_to(sandbox_root):
        sys.exit(f"Refusing to serve: {resolved} is outside the sandbox {sandbox_root}")


def _stop_when_stdin_closes(server: uvicorn.Server) -> None:
    sys.stdin.buffer.read()
    server.should_exit = True


proactor_close_fix.install()
fake_mcp_server.start_in_background(args.fake_mcp_port)

log_to_stderr = logging.StreamHandler(sys.stderr)
log_to_stderr.setLevel(logging.WARNING)
log_to_stderr.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
logging.getLogger().addHandler(log_to_stderr)

uvicorn_server = uvicorn.Server(
    uvicorn.Config(app, host=HOST, port=args.api_port, access_log=False)
)
threading.Thread(target=_stop_when_stdin_closes, args=(uvicorn_server,), daemon=True).start()
uvicorn_server.run()
# atexit 先清沙盒；Windows 上開著的記錄檔與還沒回收的資料庫連線會讓清除失敗，所以先關掉。
logging.shutdown()
gc.collect()
if not uvicorn_server.started:
    sys.exit("uvicorn did not start")
