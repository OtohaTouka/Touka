"""標準函式庫的隔離探針與技能 venv；Linux 核心測試與跨平台協定測試共用。"""

import os
import sysconfig
import venv
from pathlib import Path

EVOLUTION_PROBE_SOURCE = """
import errno
import json
import os
import sys

def _probe(path):
    try:
        os.close(os.open(path, os.O_RDONLY))
        return "ok"
    except OSError as exc:
        return errno.errorcode[exc.errno]

def _big_write(path, size):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    written, chunk = 0, b"x" * 65536
    try:
        while written < size:
            written += os.write(fd, chunk)
        return "ok"
    except OSError as exc:
        return errno.errorcode[exc.errno]
    finally:
        os.close(fd)

def _call(arguments):
    return {
        "home": os.environ.get("HOME"),
        "reads": {key: _probe(path) for key, path in arguments["paths"].items()},
        "big_write": _big_write(arguments["big"], arguments["size"]),
    }

if __name__ == "__main__":
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request["method"]
        if method == "server/discover":
            result = {
                "supportedVersions": ["2026-07-28"],
                "capabilities": {"tools": {}},
                "resultType": "complete",
                "cacheScope": "public",
                "ttlMs": 0,
            }
        elif method == "tools/list":
            result = {
                "tools": [{"name": "probe", "inputSchema": {"type": "object"}}],
                "resultType": "complete",
                "cacheScope": "public",
                "ttlMs": 0,
            }
        elif method == "tools/call":
            data = _call(request["params"]["arguments"])
            result = {
                "resultType": "complete",
                "content": [{"type": "text", "text": json.dumps(data)}],
                "structuredContent": data,
                "isError": False,
            }
        else:
            result = {}
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
"""


def build_skill_venv(python: str) -> None:
    """在演化交出的 venv python 位置（``evolution._venv_python``）建一個真的 venv.

    驗證照實跑技能資料夾裡的 python，不替換 ``_venv_python``（ADR-0293 PR 2 T3）。不裝 pip、
    不連網；套件用 ``.pth`` 把目前直譯器的 site-packages 加成 site 目錄（MCP SDK 從那裡來；那裡的
    ``.pth`` 也照常處理，例如 Windows 的 pywin32）。
    """
    root = Path(python).parent.parent
    venv.EnvBuilder(symlinks=os.name != "nt", with_pip=False).create(root)
    site = Path(sysconfig.get_path("purelib", "venv", {"base": str(root), "platbase": str(root)}))
    paths = dict.fromkeys(sysconfig.get_path(name) for name in ("purelib", "platlib"))
    lines = [f"import site; site.addsitedir({path!r})" for path in paths]
    (site / "touka_test_packages.pth").write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert Path(python).is_file(), python
