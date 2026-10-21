"""標準函式庫的隔離探針；Linux 核心測試與跨平台協定測試共用。"""

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
