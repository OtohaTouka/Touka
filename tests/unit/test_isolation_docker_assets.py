"""iso-c2 在 Owner 的 Docker 跑 Linux 測試用的資產（tests/docker）不會悄悄失效.

- 測試執行映像只補 pytest 與它的依賴，版本必須和 uv.lock 相同；
- 突變清單的每一項都還對得上程式（原文在目標檔裡剛好出現一次）、對應的測試都存在；
- 突變腳本只用標準函式庫（在容器裡以專案以外的方式執行）；
- 突變腳本給 pytest 的 ``--basetemp`` 和突變 id 無關：執行器 socket 的路徑不會因為 id 長而超過上限
  （超過時執行器不服務，不管有沒有突變測試都紅，是假紅）。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
import tomllib
from pathlib import Path, PurePosixPath

import pytest
from src.sandbox_kernel import framing
from src.services.isolation import executor_server as srv

ROOT = Path(__file__).resolve().parents[2]
DOCKER = ROOT / "tests" / "docker"
RUNNER = DOCKER / "Dockerfile.isolation-test"
MANIFEST = json.loads((DOCKER / "isolation_mutations.json").read_text(encoding="utf-8"))
# 突變只改隔離的程式：啟動器、profile／判定與 MCP 隔離傳輸
MUTABLE_PREFIXES = ("ToukaProxy/src/sandbox_kernel/", "ToukaProxy/src/services/isolation/")
MUTABLE_FILES = {
    "ToukaProxy/src/protocols/manager.py",
    "ToukaProxy/src/protocols/mcp/isolated_stdio.py",
    "ToukaProxy/src/services/mcp_isolation_store.py",
    "ToukaProxy/src/services/settings_snapshot_service.py",
    "ToukaProxy/src/tools/builtin/evolution.py",
    "ToukaProxy/src/utils/mcp_origin.py",
}

_SPEC = importlib.util.spec_from_file_location(
    "touka_isolation_mutations", DOCKER / "isolation_mutations.py"
)
assert _SPEC is not None and _SPEC.loader is not None
MUTATIONS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(MUTATIONS)
# 突變容器：--tmpfs /tmp、沒設 TMPDIR，暫存區是 /tmp/<前綴加 8 個亂數字元>
CONTAINER_TMP = PurePosixPath("/") / "tmp"
# pytest 的 tmp_path：basetemp/<測試名稱前 30 字元><序號>
# 序號照三位數算（同一輪同前綴的測試不到 1000 個）
PYTEST_TMP_NAME = "n" * 30 + "999"
# 最長的 socket：執行器 fixture 的 tmp_path/ex/ipc/ctl.sock（test_isolation_linux._executor_dirs）
EXECUTOR_SOCKET_UNDER_TMP = PurePosixPath("ex", "ipc", framing.SOCKET_NAME)
SOCKET_MARGIN_BYTES = 8


def _lf(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def test_runner_image_builds_on_the_test_proxy_image_only():
    froms = re.findall(r"(?m)^FROM\s+(\S+)", RUNNER.read_text(encoding="utf-8"))
    assert froms == ["touka-iso-test-proxy:c2"]


def test_runner_image_pins_match_the_lockfile():
    pins = dict(re.findall(r"([A-Za-z0-9_.-]+)==([0-9][^\s\\]*)", RUNNER.read_text("utf-8")))
    assert {"pytest", "pytest-asyncio", "pytest-randomly"} <= set(pins)
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = {package["name"]: package["version"] for package in lock["package"]}
    assert {name: locked.get(name) for name in pins} == pins


def test_mutation_ids_are_unique():
    ids = [item["id"] for item in MANIFEST]
    assert len(ids) == len(set(ids)) and ids


@pytest.mark.parametrize("item", MANIFEST, ids=lambda item: item["id"])
def test_each_mutation_still_matches_the_code_once(item):
    target = ROOT / item["file"]
    assert item["file"].startswith(MUTABLE_PREFIXES) or item["file"] in MUTABLE_FILES
    assert item["old"] != item["new"]
    assert _lf(target).count(item["old"]) == 1


@pytest.mark.parametrize("item", MANIFEST, ids=lambda item: item["id"])
def test_each_mutation_names_existing_tests(item):
    assert item["tests"]
    for node in item["tests"]:
        path, _, name = node.partition("::")
        assert re.search(rf"(?m)^(async )?def {re.escape(name)}\(", _lf(ROOT / path)), node


def test_the_server_subreaper_mutation_is_listed_with_its_pid_1_note():
    """伺服器不開 subreaper：pytest 是 PID 1 時孤兒本來就歸它，抓不到.

    突變腳本讓 pytest 當子行程，所以這項有沒有 ``--init`` 都會紅；直接 ``docker run`` 跑 pytest 時
    要加 ``--init``，註記要寫清楚。
    """
    [item] = [item for item in MANIFEST if item["id"] == "server_subreaper_off"]
    assert item["file"] == "ToukaProxy/src/services/isolation/executor_server.py"
    assert item["old"].endswith("SUBREAPER, 1)") and item["new"].endswith("SUBREAPER, 0)")
    setsid = "test_isolation_linux.py::test_setsid_daemon_is_killed_after_the_connection_ends"
    assert item["tests"] == [f"tests/integration/{setsid}"]
    assert "--init" in item["note"] and "PID 1" in item["note"]


def test_mutation_runner_uses_only_the_standard_library():
    tree = ast.parse((DOCKER / "isolation_mutations.py").read_text(encoding="utf-8"))
    roots = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert roots <= set(sys.stdlib_module_names) | {"__future__"}


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_multiline_mutation_matches_checkout_line_endings(tmp_path, newline):
    item = next(item for item in MANIFEST if item["id"] == "g_exception_counts_as_pass")
    target = tmp_path / "evolution.py"
    original = (item["old"] + "\n    remainder = True\n").replace("\n", newline)
    target.write_bytes(original.encode("utf-8"))
    saved = MUTATIONS._mutate(target, item["old"], item["new"])
    assert saved == original
    expected = original.replace(item["old"].replace("\n", newline),
                                item["new"].replace("\n", newline))
    assert target.read_bytes() == expected.encode("utf-8")
    target.write_bytes(saved.encode("utf-8"))
    assert target.read_bytes() == original.encode("utf-8")


def _container_basetemps(monkeypatch, tmp_path: Path, ids: list[str]) -> list[PurePosixPath]:
    """跑一次 ``main()``（不複製、不改檔、不跑 pytest），換算成容器裡每一輪的 ``--basetemp``."""
    seen: list[tuple[Path, Path]] = []

    def run(work: Path, basetemp: Path, tests: list[str]) -> tuple[int, str]:
        seen.append((work.parent, basetemp))
        return (0 if len(seen) == 1 else MUTATIONS.PYTEST_FAILED), ""

    monkeypatch.setattr(MUTATIONS, "_copy_tree", lambda dest: dest.mkdir())
    monkeypatch.setattr(MUTATIONS, "_mutate", lambda target, old, new: "")
    monkeypatch.setattr(MUTATIONS, "_run_pytest", run)
    items = [{"id": i, "file": "x.py", "old": "a", "new": "b", "tests": ["t"]} for i in ids]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(items), encoding="utf-8")
    argv = ["isolation_mutations.py", "--out", str(tmp_path / "out"), "--manifest", str(manifest)]
    monkeypatch.setattr(sys, "argv", argv)
    assert MUTATIONS.main() == 0
    assert len(seen) == len(ids) + 1  # 基準＋每一項
    return [
        CONTAINER_TMP / scratch.name / PurePosixPath(basetemp.relative_to(scratch).as_posix())
        for scratch, basetemp in seen
    ]


# id 長度 1～120（含剛好 15、16 的邊界）；120 項讓輪次跨過一位數、兩位數
ANY_LENGTH_IDS = ["m" * length for length in range(1, 121)]


def test_basetemp_has_the_baseline_length_whatever_the_mutation_id(monkeypatch, tmp_path):
    """基準全綠就證明路徑長度沒問題：每一輪的 basetemp 都和基準一樣長，和 id、輪次無關."""
    baseline, *mutated = _container_basetemps(monkeypatch, tmp_path, ANY_LENGTH_IDS)
    width = len(str(baseline).encode("utf-8"))
    assert {len(str(path).encode("utf-8")) for path in mutated} == {width}
    assert len({baseline, *mutated}) == len(ANY_LENGTH_IDS) + 1  # 每一輪各自一個 basetemp


def test_executor_socket_under_any_basetemp_stays_below_the_limit(monkeypatch, tmp_path):
    """容器裡最長的執行器 socket（含餘裕）不超過伺服器的上限；超過時伺服器判 socket_path、不服務."""
    for basetemp in _container_basetemps(monkeypatch, tmp_path, ANY_LENGTH_IDS):
        socket = basetemp / PYTEST_TMP_NAME / EXECUTOR_SOCKET_UNDER_TMP
        assert len(str(socket).encode("utf-8")) + SOCKET_MARGIN_BYTES <= srv.MAX_SOCKET_PATH_BYTES
