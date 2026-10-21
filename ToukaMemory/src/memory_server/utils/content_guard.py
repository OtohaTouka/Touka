"""Memory Content Guard (H3 fix).

Sanitizes text before it is persisted to the vector/factual stores.
Detects and flags prompt-injection attempts and enforces a maximum content
length to prevent resource exhaustion and poisoning of the knowledge base.

Design choices:
- The function WARNS and MARKS content but does NOT silently drop it, so the
  caller (save_memory) can decide the policy (store vs. reject).
- Pattern matching is conservative: false-positives on legitimate technical
  notes are better than missing injections, but we avoid over-blocking.
- No external dependencies — pure Python, fully unit-testable.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Maximum allowed character count for a single memory entry.
# Longer content is truncated rather than rejected to avoid data loss.
MAX_CONTENT_LENGTH: int = 8000

# Regex patterns that indicate prompt-injection or jailbreak attempts.
# Each pattern is compiled once at module load (performance SSOT).
_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    # Classic instruction override
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above|earlier)\s+instructions?", re.IGNORECASE),
    # Role/system override markers
    re.compile(r"\[SYSTEM\]", re.IGNORECASE),
    re.compile(r"<\|system\|>", re.IGNORECASE),
    re.compile(r"\[INST\]", re.IGNORECASE),
    re.compile(r"<\|im_start\|>", re.IGNORECASE),
    # "You are now X" identity replacement (matches "a", "an", or nothing)
    re.compile(
        r"you\s+are\s+now\s+(an?\s+)?(different|unrestricted|jailbroken|new)",
        re.IGNORECASE,
    ),
    # Disabling safety / acting without restrictions
    re.compile(
        r"(act|respond|behave)\s+(without|ignoring)\s+(any\s+)?"
        r"(safety|restriction|guideline)",
        re.IGNORECASE,
    ),
    # Newline injection: \n\nHuman: or \n\nAssistant: style
    re.compile(r"\n{2,}\s*(Human|Assistant|User|System)\s*:", re.IGNORECASE),
]


def contains_prompt_injection(content: str) -> bool:
    """只偵測提示注入，不截斷或改寫內容。

    MCP external prompt/resource 邊界需要沿用同一組模式，但其 payload 上限與記憶條目不同；
    因此提供純偵測入口，避免呼叫端複製 regex 或誤把「超過記憶長度」當成提示注入。
    """
    return any(pattern.search(content) for pattern in _INJECTION_PATTERNS)


def sanitize_memory_content(content: str) -> tuple[str, bool]:
    """Sanitize memory content before persisting to the knowledge base.

    Performs two checks:
    1. **Length truncation**: trims content to MAX_CONTENT_LENGTH characters.
    2. **Injection detection**: scans for prompt-injection / jailbreak patterns
       and logs a WARNING if any are found.

    Args:
        content: The raw memory string to sanitize.

    Returns:
        A tuple of (sanitized_content, was_modified). was_modified is True if
        the content was truncated OR a suspicious pattern was detected.
    """
    if not content:
        return content, False

    modified = False
    sanitized = content

    # --- 1. Length guard ---
    if len(sanitized) > MAX_CONTENT_LENGTH:
        sanitized = sanitized[:MAX_CONTENT_LENGTH]
        modified = True
        logger.warning(
            "⚠️ [ContentGuard] Memory content truncated from %d to %d chars.",
            len(content),
            MAX_CONTENT_LENGTH,
        )

    # --- 2. Injection pattern scan ---
    if contains_prompt_injection(sanitized):
        modified = True
        logger.warning(
            "⚠️ [ContentGuard] Potential prompt-injection detected.",
        )

    return sanitized, modified


# --- Safety flags stored next to a memory (memory lifecycle) ---------------------------
# Stored only by the system when a memory is saved; the model can never set or clear them.
SAFETY_FLAG_PROMPT_INJECTION = "prompt_injection_pattern"
# A flag column that is set but cannot be read back as a list of names (fail closed).
SAFETY_FLAG_UNREADABLE = "unreadable"


def memory_safety_flags(content: str) -> list[str]:
    """存記憶時要記下的安全標記（只標記、不改內容；截斷不算安全標記）。

    純函式：和 ``sanitize_memory_content`` 用同一組樣式，不改它的簽名與行為。
    """
    return [SAFETY_FLAG_PROMPT_INJECTION] if content and contains_prompt_injection(content) else []
