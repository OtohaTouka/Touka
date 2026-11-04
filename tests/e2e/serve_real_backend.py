"""實際驗證用的後端：在 smoke 沙盒裡跑真的 Proxy，模型只連本機的假模型（ADR-0295）.

由 ``ToukaChat/e2e-real/global-setup.ts`` 啟動；檔名不符合 smoke 的自動 discovery。

- 沙盒與外連封鎖由 ``enable_smoke_sandbox`` 負責，必須在匯入任何 ``src`` 之前。
- 標準輸入關閉就正常結束：atexit 清沙盒、把外連違規變成非 0 結束碼，所以測試要看結束碼。
"""

from __future__ import annotations

import argparse
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
args = parser.parse_args()

sandbox_root = Path(enable_smoke_sandbox("e2e_real"))
os.environ["ENABLE_DYNAMIC_MODELS"] = "true"
os.environ["SECURE_COOKIES"] = "false"
os.environ["UI_DEFAULT_URL"] = args.web_origin
os.environ["AUTH_RATE_LIMIT_MAX"] = AUTH_RATE_LIMIT_MAX_FOR_E2E
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
sys.stdout.write(f"SANDBOX_ROOT={sandbox_root}\n")
sys.stdout.flush()

sys.path[:0] = [str(REPO_ROOT / "ToukaProxy"), str(REPO_ROOT / "ToukaMemory" / "src")]

import uvicorn  # noqa: E402
from src.config import get_app_data_dir, get_config_dir  # noqa: E402
from src.main import app  # noqa: E402

for resolved in (Path(get_app_data_dir()), get_config_dir()):
    if not resolved.resolve().is_relative_to(sandbox_root):
        sys.exit(f"Refusing to serve: {resolved} is outside the sandbox {sandbox_root}")


def _stop_when_stdin_closes(server: uvicorn.Server) -> None:
    sys.stdin.buffer.read()
    server.should_exit = True


log_to_stderr = logging.StreamHandler(sys.stderr)
log_to_stderr.setLevel(logging.WARNING)
log_to_stderr.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
logging.getLogger().addHandler(log_to_stderr)

uvicorn_server = uvicorn.Server(
    uvicorn.Config(app, host=HOST, port=args.api_port, access_log=False)
)
threading.Thread(target=_stop_when_stdin_closes, args=(uvicorn_server,), daemon=True).start()
uvicorn_server.run()
# atexit 先清沙盒；Windows 上開著的記錄檔會讓清除失敗，所以先關掉。
logging.shutdown()
if not uvicorn_server.started:
    sys.exit("uvicorn did not start")
