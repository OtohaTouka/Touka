"""實際驗證用的假 MCP 伺服器：工具用表單向人要資料（ADR-0295）.

由 ``serve_real_backend.py`` 在另一個執行緒啟動，產品經真的 HTTP 連過來；
檔名不符合 pytest 與 smoke 的自動 discovery。
"""

import sys
import threading
import time

import uvicorn
from mcp import types
from mcp.server import MCPServer
from mcp.server.mcpserver import Context

HOST = "127.0.0.1"
FORM_QUESTION = "q"
STARTUP_TIMEOUT_S = 30
STARTUP_POLL_S = 0.05

# 和 ToukaChat/e2e-real/lib/constants.ts 同一句：hook 收到的內容與畫面都要看到它
FORM_MESSAGE = "實際驗證：請填這個欄位"

server = MCPServer("e2e", version="1.0.0")


@server.tool()
def ask_form(ctx: Context) -> str | types.InputRequiredResult:
    """Ask a person to fill one field and return what came back."""
    responses = ctx.input_responses
    if not responses:
        return types.InputRequiredResult(
            inputRequests={
                FORM_QUESTION: types.ElicitRequest(
                    params=types.ElicitRequestFormParams(
                        message=FORM_MESSAGE,
                        requestedSchema={
                            "type": "object",
                            "properties": {"answer": {"type": "string", "title": "答案"}},
                            "required": ["answer"],
                        },
                    )
                )
            }
        )
    answer = responses[FORM_QUESTION]
    return f"action={answer.action} content={answer.content}"


def start_in_background(port: int) -> None:
    """在 daemon 執行緒跑；跟著行程結束。"""
    uvicorn_server = uvicorn.Server(
        uvicorn.Config(
            server.streamable_http_app(json_response=True, stateless_http=True, host=HOST),
            host=HOST,
            port=port,
            access_log=False,
            log_level="warning",
        )
    )
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while not uvicorn_server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            sys.exit("Fake MCP server did not start")
        time.sleep(STARTUP_POLL_S)
