"""Isolation CI workflow: the Landlock job that really runs the kernel tests (ADR-0272).

Pins the shape the design requires so a later edit cannot quietly weaken it:

- runs on every PR event type the container job needs (``labeled`` included), on pushes to
  main/dev and on demand, with read-only permissions;
- runs natively on the runner (no ``container:``): there pytest is not pid 1, so an orphan only
  reaches the executor through ``PR_SET_CHILD_SUBREAPER`` and the "no subreaper" mutation is
  caught (in ``docker run`` pytest is pid 1 and adopts every orphan anyway);
- pass A runs the Linux module without the sysctls and without ``TOUKA_REQUIRE_LANDLOCK``;
  then the job turns Fast Open and MPTCP off; pass B runs the same module with REQUIRE;
- the junit of each pass goes through ``scripts/ci/isolation_junit_check.py``;
- nothing in the job can be skipped or ignored (no ``if``, no ``continue-on-error``);
- the container job runs by itself when a PR changes an isolation path (the scope job diffs the
  PR's merge commit against its base, no third-party action); the ``isolation`` label and a
  manual run still force it.
"""

from __future__ import annotations

import ast
import re
import shlex
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from src.services.isolation.executor_server import EXECUTOR_CODE_FILES

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/isolation.yml"
CONTRACTS = ROOT / ".github/workflows/native-tool-contracts.yml"
LINUX_MODULE = "tests/integration/test_isolation_linux.py"
CHECKER = "scripts/ci/isolation_junit_check.py"
LAUNCHER = "ToukaProxy/src/sandbox_kernel/launcher.py"
REQUIRE = "TOUKA_REQUIRE_LANDLOCK"
FASTOPEN_OFF = "sudo sysctl -w net.ipv4.tcp_fastopen=0"
MPTCP_OFF = "sudo sysctl -w net.mptcp.enabled=0"
PR_TYPES = {"opened", "reopened", "labeled", "synchronize"}
SCOPE_JOB = "container-scope"
# 容器 job 自己跑的條件：PR 改到下面任一路徑（bash case 的樣式，``*`` 也比對 ``/``）。
# 隔離程式、執行器伺服器程式（EXECUTOR_CODE_FILES）、隔離用的 utils、映像的輸入（Dockerfile、
# .dockerignore、uv.lock）、三份有執行器的 compose、端到端資產與它匯入的測試模組、CI 本身
# （第 4 輪 A1）
ISOLATION_PATHS = (
    "ToukaProxy/src/sandbox_kernel/*",
    "ToukaProxy/src/services/isolation/*",
    "ToukaProxy/src/protocols/mcp/isolated_stdio.py",
    "ToukaProxy/src/protocols/manager.py",
    "ToukaProxy/src/protocols/mcp/client.py",
    "ToukaProxy/src/protocols/mcp/environment.py",
    "ToukaProxy/src/utils/mcp_origin.py",
    "ToukaProxy/src/tools/builtin/evolution.py",
    "ToukaProxy/src/services/creation/smoke.py",
    "ToukaProxy/src/services/settings_snapshot_service.py",
    "ToukaProxy/src/routers/mcp.py",
    "ToukaProxy/src/services/execution_broker.py",
    "ToukaProxy/src/utils/isolation_policy.py",
    "ToukaProxy/src/utils/touka_dirs.py",
    "ToukaProxy/src/utils/permission_repair.py",
    "ToukaProxy/src/models/mcp_isolation.py",
    "ToukaProxy/src/services/mcp_isolation_store.py",
    "ToukaProxy/Dockerfile",
    ".dockerignore",
    "uv.lock",
    "docker-compose.yml",
    "docker-compose.ghcr.yml",
    "ToukaProxy/docker-compose.yml",
    "ToukaHeartbeat/permission_init.py",
    "tests/docker/*",
    "tests/network_guard.py",
    "tests/smoke/lib/sandbox_env.py",
    ".github/workflows/isolation.yml",
    "scripts/ci/isolation_junit_check.py",
)
# 只有 PR 事件比對：merge commit 對第一個 parent（base 的最新 commit）＝這個 PR 帶進來的改動；
# --no-renames 讓搬走的檔兩邊路徑都列出；-z 讓任何檔名都原樣比對；git 失敗時 step 失敗（bash -e）
SCOPE_STEP = [
    "changed=false",
    'if [ "$EVENT" = pull_request ]; then',
    'git diff --name-only --no-renames -z HEAD^1 HEAD > "$RUNNER_TEMP/changed"',
    "while IFS= read -r -d '' path; do",
    'case "$path" in',
    *(f"{pattern}) changed=true ;;" for pattern in ISOLATION_PATHS),
    "esac",
    'done < "$RUNNER_TEMP/changed"',
    "fi",
    'echo "isolation=$changed" >> "$GITHUB_OUTPUT"',
]
# PR 改到隔離路徑就跑；isolation 標籤或手動觸發強制跑（帶著標籤的任何 PR 事件都跑，第 3 輪 ③L2）
CONTAINER_IF = (
    f"needs.{SCOPE_JOB}.outputs.isolation == 'true'"
    " || github.event_name == 'workflow_dispatch'"
    " || contains(github.event.pull_request.labels.*.name, 'isolation')"
)
# 名稱帶這些字的程式與設定一律要在清單裡（之後新增的檔也是）
ISOLATION_WORDS = ("isolation", "landlock", "sandbox_kernel")
NAME_SCAN_ROOTS = ("ToukaProxy/src", "scripts", ".github/workflows")
COMPOSE_COPY = "$RUNNER_TEMP/compose"
GHCR_PROJECT = "touka-iso-ghcr"
E2E_COMPOSE = "tests/docker/isolation.compose.yml"
E2E_PROJECT = "touka-iso-ci"
INIT_REAL_CHECK = "tests/docker/permission_init_real.py"
ISO_OVERRIDE = "tests/docker/isolation.mptcp.override.yml"
GHCR_OVERRIDE = "tests/docker/ghcr.mptcp.override.yml"
# 核心有 MPTCP 時才帶覆寫檔（沒有 MPTCP 的核心加了 net.mptcp.enabled，容器會起不來）
MPTCP_STEP = [
    "if [ -e /proc/sys/net/mptcp/enabled ]; then",
    f'echo "ISO_MPTCP=-f $GITHUB_WORKSPACE/{ISO_OVERRIDE}" >> "$GITHUB_ENV"',
    f'echo "GHCR_MPTCP=-f $GITHUB_WORKSPACE/{GHCR_OVERRIDE}" >> "$GITHUB_ENV"',
    "else",
    'echo "no MPTCP on this kernel"',
    "fi",
]
MPTCP_SYSCTLS = {"net.ipv4.tcp_fastopen": 0, "net.mptcp.enabled": 0}
# pytest options that would run less than the whole module or hide a failure
NARROWING = {"-k", "-m", "--deselect", "--ignore", "--lf", "--sw", "-x", "--exitfirst", "||"}


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _triggers(doc: dict[str, Any]) -> dict[str, Any]:
    """YAML 1.1 reads the bare key ``on`` as True."""
    return doc["on"] if "on" in doc else doc[True]


def _job(name: str) -> dict[str, Any]:
    return _workflow()["jobs"][name]


def _commands(step: dict[str, Any]) -> list[list[str]]:
    """Each non-empty line of a run step, split like the shell would (continuations joined)."""
    text = step.get("run", "").replace("\\\n", " ")
    return [shlex.split(line) for line in text.splitlines() if line.strip()]


def _option(argv: list[str], name: str) -> str:
    """The single value of ``name`` (``--x v`` or ``--x=v``)."""
    values = [argv[index + 1] for index, arg in enumerate(argv[:-1]) if arg == name]
    values += [arg.split("=", 1)[1] for arg in argv if arg.startswith(f"{name}=")]
    assert len(values) == 1, f"{name} missing or repeated in {argv}"
    return values[0]


def _pytest_steps(steps: list[dict[str, Any]]) -> list[tuple[int, list[str]]]:
    return [
        (index, argv)
        for index, step in enumerate(steps)
        for argv in _commands(step)
        if "pytest" in argv
    ]


def _checker_runs(steps: list[dict[str, Any]], mode: str) -> list[tuple[int, list[str]]]:
    return [
        (index, argv)
        for index, step in enumerate(steps)
        for argv in _commands(step)
        if CHECKER in argv and argv[argv.index(CHECKER) + 1 :][:1] == [mode]
    ]


def _passes() -> tuple[list[dict[str, Any]], list[tuple[int, list[str]]]]:
    steps = _job("landlock")["steps"]
    runs = _pytest_steps(steps)
    assert len(runs) == 2, "the Landlock job runs the Linux module exactly twice (pass A, pass B)"
    return steps, runs


def test_triggers_cover_every_pr_event_the_container_job_needs() -> None:
    doc = _workflow()
    on = _triggers(doc)
    assert set(on["pull_request"]["types"]) == PR_TYPES
    assert set(on["push"]["branches"]) == {"main", "dev"}
    assert "workflow_dispatch" in on
    assert "pull_request_target" not in on


def test_permissions_are_read_only_and_no_secret_is_used() -> None:
    doc = _workflow()
    assert doc["permissions"] == {"contents": "read"}
    assert all("permissions" not in job for job in doc["jobs"].values())
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "secrets." not in text and "pull_request_target" not in text


def test_landlock_job_cannot_be_skipped_or_ignored() -> None:
    job = _job("landlock")
    assert "if" not in job and "continue-on-error" not in job
    for step in job["steps"]:
        assert "if" not in step and "continue-on-error" not in step, step
    assert "strategy" not in job


def test_landlock_job_runs_natively_so_pytest_is_not_pid_1() -> None:
    job = _job("landlock")
    assert job["runs-on"] == "ubuntu-24.04"
    assert "container" not in job and "services" not in job
    for _, argv in _pytest_steps(job["steps"]):
        assert argv[:6] == ["uv", "run", "--no-sync", "python", "-m", "pytest"], argv
        assert "docker" not in argv


def test_landlock_job_checks_out_and_syncs_like_the_contracts_job() -> None:
    contracts = yaml.safe_load(CONTRACTS.read_text(encoding="utf-8"))["jobs"]["contracts"]
    steps = _job("landlock")["steps"]
    for action in ("actions/checkout@", "astral-sh/setup-uv@"):
        mine = [s for s in steps if s.get("uses", "").startswith(action)]
        theirs = [s for s in contracts["steps"] if s.get("uses", "").startswith(action)]
        assert len(mine) == 1 and mine == theirs, action
    checkout = next(s for s in steps if s.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["persist-credentials"] is False
    syncs = [argv for step in steps for argv in _commands(step) if argv[:2] == ["uv", "sync"]]
    assert syncs == [["uv", "sync", "--locked", "--all-packages", "--python", "3.12"]]


def test_both_passes_run_the_whole_linux_module_the_same_way() -> None:
    _, ((_, pass_a), (_, pass_b)) = _passes()
    for argv in (pass_a, pass_b):
        options = argv[argv.index("pytest") + 1 :]
        assert options.count(LINUX_MODULE) == 1, argv
        assert not NARROWING & set(options), argv
        assert not [arg for arg in options if arg.startswith(("--deselect=", "--ignore="))], argv
    junit = "--junitxml"
    assert _option(pass_a, junit) != _option(pass_b, junit)
    strip = [arg for arg in pass_a if not arg.startswith(junit)]
    assert strip == [arg for arg in pass_b if not arg.startswith(junit)]


def test_pass_a_has_no_sysctl_and_no_require_and_pass_b_has_both() -> None:
    doc = _workflow()
    job = doc["jobs"]["landlock"]
    assert REQUIRE not in doc.get("env", {}) and REQUIRE not in job.get("env", {})
    steps, ((a_index, _), (b_index, _)) = _passes()
    assert REQUIRE not in steps[a_index].get("env", {})
    assert steps[b_index]["env"][REQUIRE] == "1"
    hardening = [
        index
        for index, step in enumerate(steps)
        if FASTOPEN_OFF in step.get("run", "") or "sysctl -w" in step.get("run", "")
    ]
    assert len(hardening) == 1, "one step turns both sysctls off"
    harden = steps[hardening[0]]["run"]
    assert a_index < hardening[0] < b_index, "harden after pass A and before pass B"
    assert FASTOPEN_OFF in harden
    assert f"if [ -e /proc/sys/net/mptcp/enabled ]; then {MPTCP_OFF}; fi" in " ".join(
        harden.split()
    )


def test_each_pass_is_checked_against_its_own_junit_and_the_host_facts() -> None:
    steps, runs = _passes()
    facts_runs = _checker_runs(steps, "facts")
    assert len(facts_runs) == 1
    facts_index, facts_argv = facts_runs[0]
    facts = _option(facts_argv, "--out")
    probes = [
        (index, argv)
        for index, step in enumerate(steps)
        for argv in _commands(step)
        if LAUNCHER in argv and "--probe" in argv
    ]
    assert len(probes) == 1 and probes[0][0] <= facts_index
    probe_argv = probes[0][1]
    assert probe_argv[:7] == ["uv", "run", "--no-sync", "python", "-I", "-S", LAUNCHER]
    for (pytest_index, pytest_argv), mode in zip(runs, ("a", "b"), strict=True):
        assert facts_index < pytest_index
        checks = _checker_runs(steps, "check")
        mine = [argv for index, argv in checks if index == pytest_index + 1]
        assert len(mine) == 1, f"pass {mode} is checked in the very next step"
        argv = mine[0]
        assert argv[:5] == ["uv", "run", "--no-sync", "python", "-I"], argv
        assert _option(argv, "--pass") == mode
        assert _option(argv, "--junit") == _option(pytest_argv, "--junitxml")
        assert _option(argv, "--facts") == facts
    assert len(_checker_runs(steps, "check")) == 2


def test_the_checker_only_parses_junit_this_job_just_wrote() -> None:
    """S314 豁免的前提：checker 只解析同一個 job 剛由 pytest 寫進 ``$RUNNER_TEMP`` 的 junit，
    而且只有這個 workflow 呼叫它（其他 workflow 不能拿它吃外部的 XML）."""
    callers = {
        path.name
        for path in (ROOT / ".github/workflows").glob("*.y*ml")
        if CHECKER in path.read_text(encoding="utf-8")
    }
    assert callers == {WORKFLOW.name}
    steps, runs = _passes()
    junits = {_option(argv, "--junitxml") for _, argv in runs}
    for _, argv in _checker_runs(steps, "check"):
        junit = _option(argv, "--junit")
        assert junit.startswith("$RUNNER_TEMP/") and junit in junits, argv


# ── 容器 job（PR 改到隔離路徑才自己跑，標籤或手動觸發強制跑；設計 §4.3）──────────────────


def _compose_files_with_an_executor() -> set[str]:
    candidates = [*ROOT.glob("docker-compose*.yml"), *ROOT.glob("*/docker-compose.yml")]
    return {
        path.relative_to(ROOT).as_posix()
        for path in candidates
        if "touka-executor"
        in (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("services", {})
    }


def _container_commands() -> list[list[str]]:
    return [argv for step in _job("container")["steps"] for argv in _commands(step)]


def test_container_job_runs_when_the_pr_changes_an_isolation_path_or_is_forced() -> None:
    """PR 改到隔離路徑就自動跑；isolation 標籤或手動觸發強制跑。條件寫在 job 的 ``if``：不跑時
    這個 check 是 skipped（不是一直 pending），workflow 層的 ``paths`` 會連 Landlock job 一起篩."""
    job = _job("container")
    assert job["needs"] == SCOPE_JOB
    assert " ".join(job["if"].split()) == CONTAINER_IF
    assert job["runs-on"] == "ubuntu-24.04" and "container" not in job
    for step in job["steps"]:
        assert "if" not in step and "continue-on-error" not in step, step
    assert "continue-on-error" not in job
    filters = [set(spec) for spec in _triggers(_workflow()).values() if spec]
    assert all(not {"paths", "paths-ignore"} & keys for keys in filters)


def test_scope_job_lists_what_the_pr_changes_against_its_base() -> None:
    """checkout 預設就是 PR 的 merge commit；抓 2 層才有它的 parent（base 的最新 commit）."""
    job = _job(SCOPE_JOB)
    assert not {"if", "continue-on-error", "needs", "container", "strategy"} & set(job)
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["outputs"] == {"isolation": "${{ steps.paths.outputs.isolation }}"}
    checkout, step = job["steps"]
    landlock_checkout = _job("landlock")["steps"][0]
    assert checkout["uses"] == landlock_checkout["uses"]
    assert checkout["with"] == {"persist-credentials": False, "fetch-depth": 2}
    assert step["id"] == "paths" and step["env"] == {"EVENT": "${{ github.event_name }}"}
    assert not {"if", "continue-on-error", "shell", "uses"} & set(step)
    assert [line.strip() for line in step["run"].splitlines() if line.strip()] == SCOPE_STEP


def _scoped(path: str) -> bool:
    """bash 的 case 比對（``*`` 也比對 ``/``），和 fnmatchcase 相同."""
    return any(fnmatchcase(path, pattern) for pattern in ISOLATION_PATHS)


def _test_modules_the_e2e_imports() -> set[str]:
    """``tests/docker`` 的檢查腳本匯入的 ``tests.*`` 模組（容器裡從唯讀掛的 repo 載入）."""
    found: set[str] = set()
    for script in (ROOT / "tests/docker").glob("*.py"):
        for node in ast.walk(ast.parse(script.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "tests":
                base = Path(*node.module.split("."))
                for alias in node.names:
                    module = base / f"{alias.name}.py"  # from tests import network_guard
                    if not (ROOT / module).is_file():
                        module = base.with_suffix(".py")  # from tests.x.y import name
                    found.add(module.as_posix())
    return found


def _named_like_isolation() -> set[str]:
    found = set()
    for top in NAME_SCAN_ROOTS:
        for path in (ROOT / top).rglob("*"):
            relative = path.relative_to(ROOT).as_posix()
            generated = "__pycache__" in path.parts or path.suffix == ".pyc"
            if path.is_file() and not generated and any(w in relative for w in ISOLATION_WORDS):
                found.add(relative)
    return found


def _utils_importing_the_kernel() -> set[str]:
    found = set()
    for path in (ROOT / "ToukaProxy/src/utils").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            module = (node.module or "") if isinstance(node, ast.ImportFrom) else ""
            if module.startswith("src.sandbox_kernel"):
                found.add(path.relative_to(ROOT).as_posix())
    return found


def test_isolation_paths_cover_what_the_container_job_builds_and_runs() -> None:
    builds = [argv for argv in _container_commands() if argv[:2] == ["docker", "build"]]
    required = {
        WORKFLOW.relative_to(ROOT).as_posix(),
        CHECKER,
        E2E_COMPOSE,
        ISO_OVERRIDE,
        GHCR_OVERRIDE,
        "uv.lock",
        ".dockerignore",
        "ToukaProxy/src/protocols/mcp/isolated_stdio.py",
        *(_option(argv, "-f") for argv in builds),
        *_compose_files_with_an_executor(),
        *(f"ToukaProxy/{name}" for name in EXECUTOR_CODE_FILES),
        *_test_modules_the_e2e_imports(),
        *_named_like_isolation(),
        *_utils_importing_the_kernel(),
    }
    # 正向對照：推導真的找到東西（掃描壞掉時這裡會紅）
    assert {
        "ToukaProxy/Dockerfile",
        "ToukaProxy/src/services/execution_broker.py",
        "tests/network_guard.py",
        "tests/smoke/lib/sandbox_env.py",
        "ToukaProxy/src/sandbox_kernel/launcher.py",
        "ToukaProxy/src/utils/touka_dirs.py",
    } <= required
    assert sorted(path for path in required if not _scoped(path)) == []


def test_every_isolation_path_still_exists_and_unrelated_changes_do_not_match() -> None:
    for pattern in ISOLATION_PATHS:
        if pattern.endswith("/*"):
            folder = ROOT / pattern.removesuffix("/*")
            assert folder.is_dir() and any(folder.iterdir()), pattern
        else:
            assert "*" not in pattern and (ROOT / pattern).is_file(), pattern
    for path in ("docs/architecture/adr/README.md", "ToukaChat/src/App.tsx", "HANDOVER.md"):
        assert not _scoped(path), path


def test_container_job_parses_every_compose_file_with_an_executor_and_an_empty_env() -> None:
    expected = _compose_files_with_an_executor()
    assert expected == {
        "docker-compose.yml",
        "docker-compose.ghcr.yml",
        "ToukaProxy/docker-compose.yml",
    }
    commands = _container_commands()
    copied = {arg for argv in commands if argv[:1] == ["cp"] for arg in argv[1:-1]}
    assert copied == expected
    parsed = {
        argv[argv.index("-f") + 1].removeprefix(f"{COMPOSE_COPY}/")
        for argv in commands
        if argv[:2] == ["docker", "compose"] and argv[-2:] == ["config", "-q"]
    }
    assert parsed == expected
    empty_envs = {argv[2] for argv in commands if argv[:2] == [":", ">"]}
    folders = {Path(COMPOSE_COPY, name).parent.as_posix() for name in expected}
    assert empty_envs == {f"{folder}/.env" for folder in folders}


def _ghcr_step(job: dict[str, Any]) -> dict[str, Any]:
    steps = [step for step in job["steps"] if "GHCR" in step.get("name", "")]
    assert len(steps) == 1
    return steps[0]


def test_container_job_starts_the_ghcr_executor_from_the_image_it_built() -> None:
    job = _job("container")
    step = _ghcr_step(job)
    ghcr = yaml.safe_load((ROOT / "docker-compose.ghcr.yml").read_text(encoding="utf-8"))
    executor = ghcr["services"]["touka-executor"]
    env = {**job.get("env", {}), **step.get("env", {})}
    # GHCR 的映像名稱代入這個 job 的 owner／version，必須正是 job 自己建的那個 tag
    image = re.sub(r"\$\{(\w+):-[^}]*\}", lambda match: env[match[1]], executor["image"])
    commands = _container_commands()
    builds = [argv for argv in commands if argv[:2] == ["docker", "build"]]
    assert builds == [["docker", "build", "-f", "ToukaProxy/Dockerfile", "-t", image, "."]]
    # skills 是 nocopy 的 subpath：空 volume 不會自動播種，先用映像播種同一個 volume
    skills = next(v for v in executor["volumes"] if isinstance(v, dict) and "volume" in v)
    assert skills["volume"]["nocopy"] is True
    # 播種要掛整個 config（skills 只是它的 subpath），路徑從 compose 推出
    target = PurePosixPath(skills["target"])
    assert target.name == skills["volume"]["subpath"]
    volume = f"{GHCR_PROJECT}_{skills['source']}:{target.parent}"
    assert ["docker", "run", "--rm", "-v", volume, image, "true"] in commands
    # create_host_path: false 的兩個資料夾與端到端要的資料夾先建好，擁有者和執行器的 user 相同
    ghcr_data = {
        Path(v["source"]).name
        for v in executor["volumes"]
        if isinstance(v, dict) and v.get("bind", {}).get("create_host_path") is False
    }
    data = sorted(f"{COMPOSE_COPY}/data/{name}" for name in ghcr_data | _e2e_data_folders())
    uid, gid = executor["user"].split(":")
    installs = [argv for argv in commands if argv[:2] == ["sudo", "install"]]
    assert installs == [["sudo", "install", "-d", "-o", uid, "-g", gid, *data]]
    assert step["working-directory"] == "${{ runner.temp }}/compose"
    compose = [
        "docker",
        "compose",
        "-p",
        GHCR_PROJECT,
        "-f",
        "docker-compose.ghcr.yml",
        "$GHCR_MPTCP",
    ]
    up = [*compose, "up", "--no-deps", "-d", "--wait", "--wait-timeout", "180", "touka-executor"]
    text = " ".join(step["run"].split())
    logs = [*compose, "logs", "touka-executor"]
    assert f"if ! {' '.join(up)}; then {' '.join(logs)} exit 1 fi" in text
    assert [*compose, "stop", "touka-executor"] in commands


def _e2e_data_folders() -> set[str]:
    """端到端從 ``TOUKA_ISO_DATA`` 底下掛的資料夾（iso-exec 的三個）。"""
    exec_volumes = _compose_doc(E2E_COMPOSE)["services"]["iso-exec"]["volumes"]
    return {
        PurePosixPath(volume.split("}", 1)[1].split(":")[0]).name
        for volume in exec_volumes
        if volume.startswith("${TOUKA_ISO_DATA")
    }


def _compose_doc(name: str) -> dict[str, Any]:
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


def _named_step(job: dict[str, Any], words: str) -> tuple[int, dict[str, Any]]:
    found = [(i, s) for i, s in enumerate(job["steps"]) if words in s.get("name", "")]
    assert len(found) == 1, words
    return found[0]


def test_container_job_runs_the_end_to_end_assets_and_cannot_swallow_a_failure() -> None:
    job = _job("container")
    index, step = _named_step(job, "End-to-end")
    builds = [argv for argv in _container_commands() if argv[:2] == ["docker", "build"]]
    assert step["env"]["TOUKA_ISO_IMAGE"] == builds[0][builds[0].index("-t") + 1]
    assert step["env"]["TOUKA_ISO_DATA"] == "${{ runner.temp }}/compose/data"
    compose = ["docker", "compose", "-p", E2E_PROJECT, "-f", E2E_COMPOSE, "$ISO_MPTCP"]
    assert _commands(step) == [
        [*compose, "up", "--abort-on-container-exit", "--exit-code-from", "iso-client"],
        [*compose, "run", "--rm", "iso-landlock"],
        [*compose, "down"],  # 不帶 -v
    ]
    data_index, _ = _named_step(job, "data folders")
    mptcp_index, _ = _named_step(job, "MPTCP")
    ghcr_index, _ = _named_step(job, "GHCR")
    assert data_index < mptcp_index < index < ghcr_index
    for each in job["steps"]:  # 失敗不能被吞掉（GitHub 的 bash 預設 -e）
        text = each.get("run", "")
        assert "||" not in text and "set +e" not in text and "shell" not in each, each


def test_container_job_runs_touka_init_on_a_real_kernel_with_the_image_it_built() -> None:
    """ADR-0279：checkout 裡的 permission_init.py 在真核心上跑（特殊項目略過、根目錄照舊嚴格）."""
    job = _job("container")
    index, step = _named_step(job, "touka-init")
    builds = [argv for argv in _container_commands() if argv[:2] == ["docker", "build"]]
    image = builds[0][builds[0].index("-t") + 1]
    assert _commands(step) == [["python3", "-I", INIT_REAL_CHECK, "--image", image]]
    build_index, _ = _named_step(job, "Build the proxy image")
    assert build_index < index
    assert (ROOT / INIT_REAL_CHECK).is_file()
    assert _scoped("ToukaHeartbeat/permission_init.py") and _scoped(INIT_REAL_CHECK)


def test_mptcp_overrides_are_added_only_when_the_kernel_has_mptcp() -> None:
    _, step = _named_step(_job("container"), "MPTCP")
    assert [line.strip() for line in step["run"].splitlines() if line.strip()] == MPTCP_STEP


def test_mptcp_overrides_cover_every_service_that_turns_fast_open_off() -> None:
    """和 README_QUICKSTART 的覆寫同形：每個關 Fast Open 的服務另關 MPTCP，兩行都寫."""
    for override, base in ((ISO_OVERRIDE, E2E_COMPOSE), (GHCR_OVERRIDE, "docker-compose.ghcr.yml")):
        expected = {
            name
            for name, service in _compose_doc(base)["services"].items()
            if "net.ipv4.tcp_fastopen" in service.get("sysctls", {})
        }
        services = _compose_doc(override)["services"]
        assert set(services) == expected and expected, override
        for name, service in services.items():
            assert service == {"sysctls": MPTCP_SYSCTLS}, (override, name)
