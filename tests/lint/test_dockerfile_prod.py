"""TDD tests for ToukaChat/Dockerfile.prod production build structure.

Verifies that the production Dockerfile follows the required patterns:
- Multi-stage build with npm run build (not npm run dev)
- Static file server in runner stage
- No terminal_server.mjs (belongs to ToukaIDE, not Docker)
- Build tools for node-pty native compilation
- Non-root user (P8 fix consistency)
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE_PROD = REPO_ROOT / "ToukaChat" / "Dockerfile.prod"


def _read() -> str:
    assert DOCKERFILE_PROD.exists(), "ToukaChat/Dockerfile.prod must exist"
    return DOCKERFILE_PROD.read_text(encoding="utf-8")


# ── Structure tests ───────────────────────────────────────────────────────────

def test_dockerfile_prod_exists():
    """Dockerfile.prod must exist as the production variant."""
    assert DOCKERFILE_PROD.exists(), (
        "ToukaChat/Dockerfile.prod is missing — create a production multi-stage Dockerfile"
    )


def test_dockerfile_prod_uses_build_not_dev():
    """Production Dockerfile must run `npm run build`, NOT `npm run dev`."""
    content = _read()
    assert "npm run build" in content, (
        "Dockerfile.prod must execute 'npm run build' to produce static output"
    )
    assert "npm run dev" not in content, (
        "Dockerfile.prod must NOT run 'npm run dev' — dev mode not suitable for production"
    )


def test_dockerfile_prod_has_static_server():
    """Runner stage must start a static file server, not Next.js dev server."""
    content = _read()
    # Accept serve, nginx, or any static server indication
    has_server = any(
        keyword in content
        for keyword in ("serve", "nginx", "http-server", "caddy", "lighttpd")
    )
    assert has_server, (
        "Dockerfile.prod must use a static file server (serve, nginx, etc.) in the runner stage"
    )


def test_dockerfile_prod_excludes_terminal_server():
    """terminal_server.mjs must NOT be executed in the production Dockerfile.

    terminal_server.mjs belongs to ToukaIDE (local machine PTY bridge).
    The production image must NOT launch it (CMD or RUN node terminal_server).
    Documentation comments mentioning it are fine.
    """
    content = _read()
    # Check only non-comment lines for functional references
    non_comment_lines = [
        line for line in content.splitlines()
        if not line.strip().startswith("#")
    ]
    non_comment_text = "\n".join(non_comment_lines)
    assert "terminal_server" not in non_comment_text, (
        "Dockerfile.prod must NOT execute terminal_server.mjs — "
        "this belongs to ToukaIDE, not the production Docker image"
    )


def test_dockerfile_prod_has_build_tools():
    """Builder stage must install python3, make, g++ for node-pty native compilation."""
    content = _read()
    assert "python3" in content, "Dockerfile.prod builder must install python3"
    assert "make" in content, "Dockerfile.prod builder must install make"
    assert "g++" in content, "Dockerfile.prod builder must install g++"


def test_dockerfile_prod_has_non_root_user():
    """Dockerfile.prod must use a non-root user (P8 fix consistency)."""
    content = _read()
    assert "USER " in content, (
        "Dockerfile.prod must switch to a non-root USER (P8 security fix)"
    )
    assert "USER root" not in content, (
        "Dockerfile.prod must NOT run as root"
    )


def test_dockerfile_prod_is_multistage():
    """Dockerfile.prod must use multi-stage build (AS builder + AS runner)."""
    content = _read()
    assert "AS builder" in content or "as builder" in content.lower(), (
        "Dockerfile.prod must have a builder stage"
    )
    assert "COPY --from=" in content, (
        "Dockerfile.prod must copy artifacts between stages (COPY --from=)"
    )


def test_dockerfile_prod_copies_source():
    """Builder stage must copy source code (COPY . .)."""
    content = _read()
    assert "COPY . ." in content or "COPY . /app" in content, (
        "Dockerfile.prod builder must copy full source code (COPY . .)"
    )
