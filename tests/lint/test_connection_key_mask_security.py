"""The browser keeps only masked connection API keys, with the placeholder the backend recognises.

ToukaChat stores the connection list with every API key replaced by a mask, and sends the
mask back for a key the user did not change. The backend keeps the stored key only when it
receives exactly its own mask (`ToukaProxy/src/utils/secrets.py`). If the two differed, the
backend would take the browser's placeholder for a new key and save it over the real one.

The list may be written only through the two masked paths: `useStoredConnections` and the
writers in `lib/connections.ts`. This is a line-based tripwire for new writers, not a parser;
renaming the key into another variable gets past it.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHAT_SRC = ROOT / "ToukaChat" / "src"
FRONTEND_MASK = CHAT_SRC / "lib" / "secretMask.ts"
BACKEND_MASK = ROOT / "ToukaProxy" / "src" / "utils" / "secrets.py"

CONNECTIONS = "lib/connections.ts"
STORED_HOOK = "hooks/useStoredConnections.ts"
WRITE_CALL = re.compile(
    r"\b(?:useLocalStorage|setLocalStorageItem|replaceLocalStorageItem)\b[^\n]*\bCONNECTIONS_STORAGE_KEY\b"
)
KEY_LITERAL = re.compile(r"""["']touka_connections["']""")
MASKED_WRITE = re.compile(
    r"\b(?:setLocalStorageItem|replaceLocalStorageItem)\(CONNECTIONS_STORAGE_KEY, masked\)"
)
MASKED_HOOK = re.compile(
    r"\buseLocalStorage<Connection\[\]>\(CONNECTIONS_STORAGE_KEY, \[\], maskConnectionKeys\)"
)


def _product_sources() -> dict[str, str]:
    sources = {}
    for path in sorted([*CHAT_SRC.rglob("*.ts"), *CHAT_SRC.rglob("*.tsx")]):
        relative = path.relative_to(CHAT_SRC)
        if "__tests__" in relative.parts or relative.parts[0] == "test" or ".test." in path.name:
            continue
        sources[relative.as_posix()] = path.read_text(encoding="utf-8")
    return sources


def _storage_violations(sources: dict[str, str]) -> list[str]:
    problems = []
    for name, text in sources.items():
        for number, line in enumerate(text.splitlines(), 1):
            where = f"{name}:{number}"
            if KEY_LITERAL.search(line) and name != CONNECTIONS:
                problems.append(
                    f"{where} spells the storage key instead of using CONNECTIONS_STORAGE_KEY"
                )
            if not WRITE_CALL.search(line):
                continue
            if name == CONNECTIONS and MASKED_WRITE.search(line):
                continue
            if name == STORED_HOOK and MASKED_HOOK.search(line):
                continue
            problems.append(f"{where} writes the connection list without masking its keys")
    return problems


def test_browser_mask_is_the_backend_mask() -> None:
    frontend = re.search(
        r'(?m)^export const SECRET_MASK = "([^"]+)";$', FRONTEND_MASK.read_text(encoding="utf-8")
    )
    backend = re.search(r'(?m)^MASK = "([^"]+)"$', BACKEND_MASK.read_text(encoding="utf-8"))

    assert frontend is not None, "ToukaChat/src/lib/secretMask.ts no longer declares SECRET_MASK"
    assert backend is not None, "utils/secrets.py no longer declares MASK as a literal"
    assert frontend.group(1) == backend.group(1)


def test_only_masked_writers_touch_the_stored_connection_list() -> None:
    sources = _product_sources()

    assert CONNECTIONS in sources and STORED_HOOK in sources
    assert _storage_violations(sources) == []


def test_the_writer_check_reports_unmasked_writes() -> None:
    sources = {
        CONNECTIONS: "    setLocalStorageItem(CONNECTIONS_STORAGE_KEY, conns);\n",
        STORED_HOOK: "    return useLocalStorage<Connection[]>(CONNECTIONS_STORAGE_KEY, []);\n",
        "components/Other.tsx": (
            "const [list, setList] = useLocalStorage(CONNECTIONS_STORAGE_KEY, []);\n"
            "const raw = getLocalStorageItem('touka_connections', []);\n"
        ),
    }

    assert _storage_violations(sources) == [
        f"{CONNECTIONS}:1 writes the connection list without masking its keys",
        f"{STORED_HOOK}:1 writes the connection list without masking its keys",
        "components/Other.tsx:1 writes the connection list without masking its keys",
        "components/Other.tsx:2 spells the storage key instead of using CONNECTIONS_STORAGE_KEY",
    ]
