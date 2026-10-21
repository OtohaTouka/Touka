"""Child-only probe: leave a Qdrant local-mode client open in the session sandbox.

test_session_sandbox_no_residue runs this file by explicit path in a child
pytest; the name does not match python_files, so normal collection skips it.
Only the Qdrant client is opened (it holds vault/_qdrant_db/.lock); no
embedding model is loaded or downloaded.
"""

from __future__ import annotations

from pathlib import Path

from memory_server.vector import VectorStore


def test_open_qdrant_client_in_session_sandbox(global_test_sandbox: str) -> None:
    vault = Path(global_test_sandbox) / "vault"

    VectorStore(base_dir=vault)

    assert (vault / "_qdrant_db" / ".lock").is_file()
