"""P3 isolated legacy stdio peer；刻意不 import MCP SDK。"""

from __future__ import annotations

import json
import sys
from typing import Any


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


for line in sys.stdin:
    message = json.loads(line)
    request_id = message.get("id")
    method = message.get("method")
    if request_id is None:
        continue
    if method == "server/discover":
        response = _error(request_id, -32601, "Method not found")
    elif method == "initialize":
        response = _result(
            request_id,
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {
                    "tools": {},
                    "resources": {},
                    "prompts": {},
                },
                "serverInfo": {"name": "p3-legacy-stdio", "version": "1.0.0"},
            },
        )
    elif method == "tools/list":
        response = _result(
            request_id,
            {
                "tools": [
                    {
                        "name": "legacy_ping",
                        "description": "Legacy matrix marker.",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ]
            },
        )
    elif method == "resources/list":
        response = _result(
            request_id,
            {
                "resources": [
                    {
                        "name": "p5_readme",
                        "title": "P5 readme",
                        "uri": "touka-resource://p5/readme",
                        "description": "Deterministic P5 resource marker.",
                        "mimeType": "text/plain",
                    }
                ]
            },
        )
    elif method == "resources/templates/list":
        response = _result(
            request_id,
            {
                "resourceTemplates": [
                    {
                        "name": "p5_item",
                        "title": "P5 item",
                        "uriTemplate": "touka-resource://p5/items/{item_id}",
                        "description": "Parameterized P5 resource template.",
                        "mimeType": "text/plain",
                    }
                ]
            },
        )
    elif method == "resources/read":
        uri = message.get("params", {}).get("uri")
        response = _result(
            request_id,
            {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "text/plain",
                        "text": "P5 resource marker.",
                    }
                ]
            },
        )
    elif method == "prompts/list":
        response = _result(
            request_id,
            {
                "prompts": [
                    {
                        "name": "p5_review",
                        "title": "P5 review",
                        "description": "Build a deterministic review request.",
                        "arguments": [
                            {
                                "name": "topic",
                                "description": "Review topic.",
                                "required": True,
                            }
                        ],
                    }
                ]
            },
        )
    elif method == "prompts/get":
        topic = message.get("params", {}).get("arguments", {}).get("topic", "")
        response = _result(
            request_id,
            {
                "description": "Build a deterministic review request.",
                "messages": [
                    {
                        "role": "user",
                        "content": {
                            "type": "text",
                            "text": f"Review {topic}",
                        },
                    }
                ],
            },
        )
    else:
        response = _error(request_id, -32601, "Method not found")
    sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
    sys.stdout.flush()
