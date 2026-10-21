"""P3 modern stdio peer：真 MCPServer／server-discover／tools-list。"""

from mcp.server import MCPServer

mcp = MCPServer("p3-modern-stdio", version="2.0.0")


@mcp.tool()
def p3_ping() -> str:
    """Return a deterministic P3 marker."""
    return "pong"


@mcp.resource(
    "touka-resource://p5/readme",
    name="p5_readme",
    title="P5 readme",
    description="Deterministic P5 resource marker.",
    mime_type="text/plain",
)
def p5_readme() -> str:
    return "P5 resource marker."


@mcp.resource(
    "touka-resource://p5/items/{item_id}",
    name="p5_item",
    title="P5 item",
    description="Parameterized P5 resource template.",
    mime_type="text/plain",
)
def p5_item(item_id: str) -> str:
    return f"P5 item {item_id}"


@mcp.prompt(
    name="p5_review",
    title="P5 review",
    description="Build a deterministic review request.",
)
def p5_review(topic: str) -> str:
    return f"Review {topic}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
