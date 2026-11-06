"""iso-c2 C2-2：執行器容器 ``touka-executor`` 的部署接線（Dockerfile 與三份 compose；靜態）.

代理人指令真正在這個容器裡執行，所以它的部署設定本身就是安全邊界：

- 不帶 ``env_file``，environment 只有允許的名稱（沒有金鑰與 Touka 設定）；服務的鍵用允許清單
  （``ports``、``cap_add``、``privileged``、``pid``／``ipc``／``network_mode``、``devices``、
  ``volumes_from``、``extra_hosts``、``build`` 等一律不准）；
- 只掛兩個資料夾（可寫）、skills（唯讀）、ipc 與它自己的 state；不掛 ``.env``、原始碼、其他設定、
  工作區、``docker.sock``；兩個新 volume 只出現在該出現的服務上；
- 只接 ``touka-exec-net``，不接 Touka 內部網路；沒有發布任何埠（整份 compose 仍是 4 個）；
- 和 proxy 同一個映像、uid 1001、唯讀 rootfs、``cap_drop: ALL``、``no-new-privileges``、``init``；
- sysctl 只有關掉 TCP Fast Open（MPTCP 只能由 Owner 依核心加在覆寫檔）；
- healthcheck 同時要 socket 與新鮮的 ``alive``（只看 socket 會被 volume 裡殘留的舊 socket 騙過）；
- command 用伺服器自己的命令列解析器解析後，ipc、state、kernel、roots 和掛載、proxy 的預期
  （``expected_executor_roots``）、Dockerfile 一致，而且帶 ``--require-foreign-peer``；
- 映像預建 ipc 與 state（appuser、0700：空 volume 第一次掛載時照映像複製擁有者）；git 另成一層。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pytest
import yaml
from src.sandbox_kernel import framing
from src.services.isolation import executor_server, executor_session
from src.services.isolation.profiles import SandboxLayout
from src.services.isolation.status import expected_executor_roots

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = (ROOT / "ToukaProxy" / "Dockerfile").read_text(encoding="utf-8")
ENV_EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")
COMPOSES = ("docker-compose.yml", "docker-compose.ghcr.yml", "ToukaProxy/docker-compose.yml")
LOCAL_BUILDS = ("docker-compose.yml", "ToukaProxy/docker-compose.yml")  # 映像在本機建，不拉
GHCR = "docker-compose.ghcr.yml"
EXECUTOR, PROXY = "touka-executor", "touka-proxy"
EXEC_NET, IPC_VOLUME, STATE_VOLUME = "touka-exec-net", "touka-exec-ipc", "touka-executor-state"
MODULE = "src.services.isolation.executor_server"
FOREIGN_PEER = "--require-foreign-peer"
FAST_OPEN_OFF = {"net.ipv4.tcp_fastopen": 0}
HARDENING = {
    "user": "1001:1001",
    "init": True,
    "read_only": True,
    "cap_drop": ["ALL"],
    "security_opt": ["no-new-privileges:true"],
    "restart": "unless-stopped",
    "container_name": EXECUTOR,
}
LIMITS = {
    "mem_limit": "TOUKA_EXECUTOR_MEM_LIMIT",
    "pids_limit": "TOUKA_EXECUTOR_PIDS_LIMIT",
    "cpus": "TOUKA_EXECUTOR_CPUS",
}
# 執行器服務只能有這些鍵；沒列的一律不准（env_file、ports、expose、privileged、cap_add、pid、
# ipc、network_mode、devices、volumes_from、extra_hosts、build、working_dir、entrypoint…）
OPTIONAL_KEYS = frozenset({"pull_policy", "depends_on"})
REQUIRED_KEYS = frozenset(
    {"image", "command", "tmpfs", "environment", "volumes", "networks", "sysctls", "healthcheck"}
    | set(HARDENING)
    | set(LIMITS)
)
ALLOWED_ENV = frozenset({"TZ", "PYTHONDONTWRITEBYTECODE"})
# 掛載來源與目標裡絕對不能出現的東西（金鑰、原始碼、Docker 遠端控制）
FORBIDDEN_MOUNT_FRAGMENTS = ("docker.sock", ".env", "host_root", "workspace", "/src")
# 伺服器 listen 前做兩次自測（9p 與 state），各有逾時；start_period 要多留這麼多秒給 import 與 bind
SELFTESTS_BEFORE_LISTEN, START_MARGIN_SECONDS = 2, 10
DURATION_UNITS = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}


@dataclass(frozen=True)
class Mount:
    """一筆掛載（short 或 long syntax 正規化後）。"""

    kind: str  # "bind" 或 "volume"
    source: str
    target: str
    read_only: bool
    subpath: str | None = None
    nocopy: bool | None = None
    create_host_path: bool | None = None


def _compose(rel: str) -> dict:
    return yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))


def _service(rel: str, name: str) -> dict:
    return _compose(rel)["services"][name]


def _mounts(service: dict) -> list[Mount]:
    found = []
    for item in service.get("volumes", []):
        if isinstance(item, dict):
            volume, bind = item.get("volume", {}), item.get("bind", {})
            found.append(
                Mount(
                    item["type"], str(item["source"]), item["target"],
                    bool(item.get("read_only", False)), volume.get("subpath"),
                    volume.get("nocopy"), bind.get("create_host_path"),
                )
            )
            continue
        cut = item.find(":/")
        source, (target, _, mode) = item[:cut], item[cut + 1 :].partition(":")
        kind = "bind" if source.startswith((".", "/", "$")) else "volume"
        found.append(Mount(kind, source, target, "ro" in mode.split(",")))
    return found


def _env(service: dict) -> dict[str, str]:
    raw = service.get("environment", {})
    if isinstance(raw, dict):
        return {key: str(value) for key, value in raw.items()}
    return dict(item.split("=", 1) for item in raw)


def _mount_at(service: dict, target: str) -> Mount:
    (mount,) = [m for m in _mounts(service) if m.target == target]
    return mount


def _proxy_data(rel: str) -> PurePosixPath:
    """Proxy 看到的資料根（``MEMORY_APP_DATA_DIR``，也是 proxy 掛 data 的位置）。"""
    return PurePosixPath(_env(_service(rel, PROXY))["MEMORY_APP_DATA_DIR"])


def _proxy_config(rel: str) -> PurePosixPath:
    """Proxy 的設定目錄（``config/skills`` 的上層）：掛在 ``ToukaProxy/config`` 的那筆。"""
    (target,) = [
        m.target for m in _mounts(_service(rel, PROXY)) if m.target.endswith("/ToukaProxy/config")
    ]
    return PurePosixPath(target)


def _parsed_command(rel: str):
    command = _service(rel, EXECUTOR)["command"]
    assert command[:3] == ["python", "-m", MODULE]
    return executor_server.parse_cli(command[3:])


def _kernel_dir_in_image() -> str:
    return re.search(r"(?m)^ENV TOUKA_SANDBOX_KERNEL_DIR=(\S+)$", DOCKERFILE)[1]


def _users_of(rel: str, volume: str) -> set[str]:
    return {
        name
        for name, service in _compose(rel)["services"].items()
        if any(m.kind == "volume" and m.source == volume for m in _mounts(service))
    }


def _compose_literal(value: str) -> str:
    """compose 的字串在解析後的樣子；``$`` 只能是 ``$$``（未跳脫的 ``$(`` 會讓 config 失敗）。"""
    assert "$" not in value.replace("$$", ""), value
    return value.replace("$$", "$")


def _seconds(duration: str) -> float:
    """Compose 的時間長度（例如 ``70s``、``1m10s``）換成秒."""
    parts = re.findall(r"(\d+(?:\.\d+)?)(ms|h|m|s)", duration)
    assert parts and "".join(number + unit for number, unit in parts) == duration, duration
    return sum(float(number) * DURATION_UNITS[unit] for number, unit in parts)


def _run_lines() -> list[str]:
    joined = re.sub(r"\\\r?\n\s*", " ", DOCKERFILE)
    return re.findall(r"(?m)^RUN\s+(.+)$", joined)


def _dockerfile_line(pattern: str) -> int:
    match = re.search(pattern, DOCKERFILE, re.MULTILINE)
    assert match, pattern
    return DOCKERFILE[: match.start()].count("\n")


# ── 服務的形狀 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("rel", COMPOSES)
def test_executor_service_has_only_allowed_keys(rel):
    keys = set(_service(rel, EXECUTOR))
    assert keys - REQUIRED_KEYS - OPTIONAL_KEYS == set()
    assert keys >= REQUIRED_KEYS


@pytest.mark.parametrize("rel", COMPOSES)
def test_executor_reads_no_env_file_and_gets_no_settings(rel):
    """執行器不讀 .env：金鑰、token、Touka 設定一個都不能進這個容器。"""
    executor = _service(rel, EXECUTOR)
    assert "env_file" not in executor
    assert set(_env(executor)) <= ALLOWED_ENV


@pytest.mark.parametrize("rel", COMPOSES)
def test_executor_runs_the_proxy_image_unprivileged(rel):
    executor, proxy = _service(rel, EXECUTOR), _service(rel, PROXY)
    assert executor["image"] == proxy["image"]
    assert {key: executor[key] for key in HARDENING} == HARDENING
    (tmpfs,) = executor["tmpfs"]  # 唯讀 rootfs 唯一可寫的暫存，大小有上限
    assert "size=" in tmpfs
    for key, variable in LIMITS.items():
        assert re.fullmatch(rf"\$\{{{variable}:-[0-9a-z.]+\}}", str(executor[key])), key
    if rel in LOCAL_BUILDS:  # proxy 建的本機映像；不能被拉成同名的別人映像
        assert executor["pull_policy"] == "never" and "build" in proxy
    else:
        registry, slash, _ = executor["image"].partition("/")
        assert "pull_policy" not in executor and slash and registry == "ghcr.io"


@pytest.mark.parametrize("rel", COMPOSES)
def test_executor_joins_only_its_own_network(rel):
    compose = _compose(rel)
    assert _service(rel, EXECUTOR)["networks"] == [EXEC_NET]
    assert EXEC_NET in compose["networks"]
    on_exec_net = {
        name for name, svc in compose["services"].items() if EXEC_NET in svc.get("networks", [])
    }
    assert on_exec_net == {EXECUTOR}


@pytest.mark.parametrize("rel", COMPOSES)
def test_executor_publishes_no_ports(rel):
    services = _compose(rel)["services"]
    assert not {"ports", "expose"} & set(services[EXECUTOR])
    if rel != "ToukaProxy/docker-compose.yml":  # 兩份正式設定對外仍只有 4 個埠
        assert sum(len(svc.get("ports", [])) for svc in services.values()) == 4


@pytest.mark.parametrize("rel", COMPOSES)
def test_executor_sysctls_only_turn_off_tcp_fast_open(rel):
    assert _service(rel, EXECUTOR)["sysctls"] == FAST_OPEN_OFF


# ── 掛載與 volume ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("rel", COMPOSES)
def test_executor_mounts_only_two_data_folders_skills_ipc_and_state(rel):
    executor, config = _service(rel, EXECUTOR), _parsed_command(rel)
    mounts = _mounts(executor)
    targets = [m.target for m in mounts]
    assert len(targets) == len(set(targets)) == 5
    assert set(targets) == {*config.rw, *config.ro, config.ipc, config.state}
    for mount in mounts:
        for text in (mount.source, mount.target):
            assert not [bad for bad in FORBIDDEN_MOUNT_FRAGMENTS if bad in text], mount
    assert "docker.sock" not in yaml.safe_dump(executor)


@pytest.mark.parametrize("rel", COMPOSES)
def test_data_roots_are_the_proxys_own_data_folders_and_writable(rel):
    """和 proxy 同一個實體資料夾、同一個路徑.

    前者是個人空間目錄 ``.touka`` 的前提（ADR-0255），後者讓 READY roots 對得上。
    """
    executor, data = _service(rel, EXECUTOR), _proxy_data(rel)
    proxy_data = _mount_at(_service(rel, PROXY), str(data))
    for name in ("personal_drive", "mcp_servers/managed"):  # 只掛 managed（ADR-0293 PR 2 Q7）
        mount = _mount_at(executor, str(data / name))
        assert mount.kind == "bind" and not mount.read_only
        assert mount.source == f"{proxy_data.source}/{name}"


@pytest.mark.parametrize("rel", COMPOSES)
def test_skills_are_read_only(rel):
    skills = _mount_at(_service(rel, EXECUTOR), str(_proxy_config(rel) / "skills"))
    assert skills.read_only


@pytest.mark.parametrize("rel", COMPOSES)
def test_ipc_and_state_volumes_are_mounted_only_where_they_belong(rel):
    volumes = _compose(rel)["volumes"]
    assert IPC_VOLUME in volumes and STATE_VOLUME in volumes
    assert _users_of(rel, IPC_VOLUME) == {PROXY, EXECUTOR}
    assert _users_of(rel, STATE_VOLUME) == {EXECUTOR}
    config = _parsed_command(rel)
    executor = _service(rel, EXECUTOR)
    assert _mount_at(executor, config.ipc) == Mount("volume", IPC_VOLUME, config.ipc, False)
    assert _mount_at(executor, config.state) == Mount("volume", STATE_VOLUME, config.state, False)


@pytest.mark.parametrize("rel", COMPOSES)
def test_proxy_finds_the_executor_through_the_shared_ipc_without_waiting_for_it(rel):
    """Proxy 經同一個 ipc 找執行器；不 depends_on 它（順序靠背景重探）。"""
    proxy, ipc = _service(rel, PROXY), _parsed_command(rel).ipc
    assert _env(proxy)["TOUKA_EXECUTOR_DIR"] == ipc
    assert _mount_at(proxy, ipc) == Mount("volume", IPC_VOLUME, ipc, False)
    assert EXECUTOR not in proxy.get("depends_on", {})


# ── command 與伺服器的命令列 ──────────────────────────────────────────────────


@pytest.mark.parametrize("rel", COMPOSES)
def test_command_requires_a_foreign_peer(rel):
    assert _parsed_command(rel).require_foreign_peer is True
    assert _service(rel, EXECUTOR)["command"].count(FOREIGN_PEER) == 1


@pytest.mark.parametrize("rel", COMPOSES)
def test_command_roots_are_exactly_what_the_proxy_expects(rel):
    """Proxy 判定執行器層時 READY 的 roots 要和 ``expected_executor_roots`` 完全相同."""
    config, data = _parsed_command(rel), _proxy_data(rel)
    layout = SandboxLayout(
        data_root=data, config_dir=_proxy_config(rel), install_root=data.parent,
        proxy_root=data.parent, env_file=data.parent / ".env", state_root=None, kernel_dir=None,
        executor_dir=PurePosixPath(config.ipc), mirror_roots=(), in_container=True,
    )
    expected = {(root.path, root.access) for root in expected_executor_roots(layout)}
    actual = {(path, "rw") for path in config.rw} | {(path, "ro") for path in config.ro}
    assert actual == expected
    kinds = {name for name, _middle in executor_session.KIND_LAYOUT.values()}
    assert {PurePosixPath(path).name for path in config.rw} == kinds


@pytest.mark.parametrize("rel", COMPOSES)
def test_command_kernel_is_the_images_read_only_copy(rel):
    assert _parsed_command(rel).kernel == _kernel_dir_in_image()


def test_env_example_documents_the_executor_limits_with_the_compose_defaults():
    """註解掉的預設值：設定頁（``/config/env`` 只列沒註解的鍵）不列，複製成 .env 也不會寫死."""
    executor = _service("docker-compose.yml", EXECUTOR)
    defaults = {
        variable: re.fullmatch(rf"\$\{{{variable}:-(.+)\}}", str(executor[key]))[1]
        for key, variable in LIMITS.items()
    }
    documented = dict(re.findall(r"(?m)^# (TOUKA_EXECUTOR_[A-Z_]+)=(\S+)$", ENV_EXAMPLE))
    assert documented == defaults
    assert not re.search(r"(?m)^\s*TOUKA_EXECUTOR_", ENV_EXAMPLE)


def test_all_profiles_run_the_same_command_and_hardening():
    shapes = {
        rel: {
            key: _service(rel, EXECUTOR)[key]
            for key in ("command", "tmpfs", "sysctls", "healthcheck", *HARDENING, *LIMITS)
        }
        for rel in COMPOSES
    }
    assert len({repr(shape) for shape in shapes.values()}) == 1


# ── healthcheck ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("rel", COMPOSES)
def test_healthcheck_needs_the_socket_and_a_fresh_alive_file(rel):
    """殘留的舊 socket 騙不過：還要 ``alive`` 在一分鐘內更新過（伺服器每 5 秒更新一次）."""
    healthcheck, ipc = _service(rel, EXECUTOR)["healthcheck"], _parsed_command(rel).ipc
    kind, command = healthcheck["test"]
    assert kind == "CMD-SHELL"
    parts = [part.strip() for part in _compose_literal(command).split("&&")]
    socket_path = PurePosixPath(ipc, framing.SOCKET_NAME)
    alive_path = PurePosixPath(ipc, executor_server.ALIVE_NAME)
    assert parts == [f"test -S {socket_path}", f'test -n "$(find {alive_path} -mmin -1)"']
    assert executor_server.MAINTAIN_SECONDS < 60
    assert healthcheck["interval"] and healthcheck["start_period"]


@pytest.mark.parametrize("rel", COMPOSES)
def test_healthcheck_start_period_covers_both_selftests(rel):
    """剛升級時兩次自測還在跑，``docker ps`` 不該先顯示 unhealthy（Owner 會誤會）."""
    start_period = _seconds(_service(rel, EXECUTOR)["healthcheck"]["start_period"])
    needed = SELFTESTS_BEFORE_LISTEN * executor_server.SELFTEST_SECONDS + START_MARGIN_SECONDS
    assert start_period >= needed


# ── 每份 compose 自己的差異 ───────────────────────────────────────────────────


def test_dev_executor_waits_for_init_which_creates_the_data_folders():
    """開發版：init 先建兩個資料夾再 chown，Docker 才不會替 bind 建出 root 擁有的資料夾."""
    services = _compose("docker-compose.yml")["services"]
    assert services[EXECUTOR]["depends_on"] == {
        "touka-init": {"condition": "service_completed_successfully"}
    }
    script = services["touka-init"]["command"].strip().removeprefix('sh -c "')
    segments = [seg.strip() for seg in script.split(";")]
    rw_roots = _parsed_command("docker-compose.yml").rw  # init 和執行器看到同一個 data 掛載點
    mkdir = segments.index(f"mkdir -p {' '.join(rw_roots)}")
    chown = next(i for i, seg in enumerate(segments) if seg.startswith("chown -R"))
    assert mkdir < chown


def test_ghcr_executor_uses_existing_folders_and_the_seeded_config_subpath():
    """GHCR：資料夾不讓 Docker 代建；skills 取自 proxy 已播種的 ``proxy-config``（唯讀子路徑）."""
    executor = _service(GHCR, EXECUTOR)
    data = _proxy_data(GHCR)
    for name in ("personal_drive", "mcp_servers/managed"):  # 只掛 managed（ADR-0293 PR 2 Q7）
        assert _mount_at(executor, str(data / name)).create_host_path is False
    skills = _mount_at(executor, str(_proxy_config(GHCR) / "skills"))
    assert (skills.kind, skills.source, skills.subpath) == ("volume", "proxy-config", "skills")
    assert skills.read_only and skills.nocopy is True
    assert executor["depends_on"] == {PROXY: {"condition": "service_healthy"}}
    version = re.search(r"Docker Compose v2\.(\d+)\+", (ROOT / GHCR).read_text(encoding="utf-8"))
    assert version and int(version[1]) >= 26  # volume subpath 需要 Compose 2.26


def test_toukaproxy_dev_compose_keeps_the_proxy_on_its_default_network():
    services = _compose("ToukaProxy/docker-compose.yml")["services"]
    assert "networks" not in services[PROXY]
    assert "depends_on" not in services[EXECUTOR]


# ── Dockerfile ───────────────────────────────────────────────────────────────


def test_image_precreates_private_ipc_and_executor_state_dirs():
    """空 volume 第一次掛載時照映像複製擁有者與權限：伺服器要求兩個資料夾都是自己的、0700."""
    config = _parsed_command("docker-compose.yml")
    installs = [
        line.split("install -d -o appuser -g appuser -m 0700 ", 1)[1].split("&&")[0].split()
        for line in _run_lines()
        if "install -d -o appuser -g appuser -m 0700 " in line
    ]
    assert len(installs) == 1
    assert {config.ipc, config.state} <= set(installs[0])


def test_git_is_its_own_layer_between_the_venv_and_copy_all():
    """改 git 不會讓 venv 那層的快取失效；git 不和基底套件裝在同一層."""
    git = (
        "apt-get update && apt-get install -y --no-install-recommends git"
        " && rm -rf /var/lib/apt/lists/*"
    )
    assert [line for line in _run_lines() if re.search(r"\bgit\b", line)] == [git]
    layer = _dockerfile_line(rf"^RUN {re.escape(git)}$")
    assert _dockerfile_line(r"^\s+uv sync --locked") < layer
    assert layer < _dockerfile_line(r"^COPY --chown=appuser:appuser \. \.$")


# ── MPTCP 覆寫檔 ─────────────────────────────────────────────────────────────────


def test_the_mptcp_override_file_the_readme_names_is_git_ignored():
    """README 教 Owner 在 repo 根放 ``docker-compose.override.yml``：``git add -A`` 不會帶進去."""
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "/docker-compose.override.yml" in {line.strip() for line in lines}
