"""iso-c2：隔離信任根與 sandbox state 的部署接線（Dockerfile、.dockerignore、三份 compose；靜態）.

- 映像在 ``COPY . .`` 之外另存一份唯讀、root 擁有的 ``sandbox_kernel``，並以 env 指出位置；
- 映像預建 sandbox state 目錄（appuser、0700），三份 compose 都把 named volume
  ``touka-sandbox-state`` 掛在同一個位置並設 ``TOUKA_SANDBOX_STATE_DIR``；
- 開發版 init 只對 volume 根目錄 chown／chmod（不遞迴）；GHCR 的 permission_init 不加角色；
- 三份 compose 的 proxy 都關掉 TCP Fast Open，都不設 MPTCP；
- 映像的安裝根目錄不留下開發用設定檔（代理人往上找不到任何設定檔）。
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

import pytest
import yaml
from src.sandbox_kernel import launcher

from tests.unit.test_docker_build_inputs import _finite_context_included

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = (ROOT / "ToukaProxy" / "Dockerfile").read_text(encoding="utf-8")
IGNORE_RULES = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
VOLUME = "touka-sandbox-state"
STATE_ENV = "TOUKA_SANDBOX_STATE_DIR"
KERNEL_ENV = "TOUKA_SANDBOX_KERNEL_DIR"
COMPOSES = ("docker-compose.yml", "docker-compose.ghcr.yml", "ToukaProxy/docker-compose.yml")
DEV_CONFIGS = frozenset(
    {"pytest.ini", "ruff.toml", ".python-version", ".importlinter", "mkdocs.yml"}
    | {"rebuildable-outputs.txt", "pyproject.toml", "uv.lock"}
)
INSTALL_ROOT = PurePosixPath(re.search(r"(?m)^WORKDIR\s+(\S+)", DOCKERFILE)[1])


def _services(rel: str) -> dict:
    return yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))["services"]


def _mounts(service: dict) -> list[tuple[str, str, str]]:
    """(種類, 來源, 目標)；種類是 volume 或 bind。"""
    found = []
    for item in service.get("volumes", []):
        if isinstance(item, dict):
            found.append((item["type"], str(item["source"]), item["target"]))
            continue
        cut = item.find(":/")
        source, target = item[:cut], item[cut + 1 :].split(":")[0]
        kind = "bind" if source.startswith((".", "/", "$")) else "volume"
        found.append((kind, source, target))
    return found


def _env(service: dict) -> dict[str, str]:
    raw = service.get("environment", {})
    if isinstance(raw, dict):
        return {k: str(v) for k, v in raw.items()}
    return dict(item.split("=", 1) for item in raw)


def _state_dir(rel: str) -> PurePosixPath:
    return PurePosixPath(_env(_services(rel)["touka-proxy"])[STATE_ENV])


def _line_index(pattern: str) -> int:
    match = re.search(pattern, DOCKERFILE, re.MULTILINE)
    assert match, pattern
    return DOCKERFILE[: match.start()].count("\n")


@pytest.mark.parametrize("rel", COMPOSES)
def test_proxy_mounts_the_named_state_volume_where_the_env_points(rel):
    compose = yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))
    state = _state_dir(rel)
    assert state.is_absolute()
    assert VOLUME in compose.get("volumes", {})
    mounts = _mounts(compose["services"]["touka-proxy"])
    assert ("volume", VOLUME, str(state)) in mounts


@pytest.mark.parametrize("rel", COMPOSES)
def test_state_dir_is_its_own_mount_under_the_install_root(rel):
    state = _state_dir(rel)
    assert state.parent == INSTALL_ROOT
    mounts = _mounts(_services(rel)["touka-proxy"])
    others = [PurePosixPath(target) for _kind, source, target in mounts if source != VOLUME]
    assert others
    for target in others:  # 不在 data／workspace／host_root 等其他掛載裡，也不包住它們
        assert not state.is_relative_to(target) and not target.is_relative_to(state)


@pytest.mark.parametrize("rel", COMPOSES)
def test_only_proxy_and_dev_init_mount_the_state_volume(rel):
    users = {
        name
        for name, service in _services(rel).items()
        if any(source == VOLUME for _kind, source, _target in _mounts(service))
    }
    expected = {"touka-proxy", "touka-init"} if rel == "docker-compose.yml" else {"touka-proxy"}
    assert users == expected


@pytest.mark.parametrize("rel", COMPOSES)
def test_proxy_turns_off_tcp_fast_open_and_never_sets_mptcp(rel):
    """Landlock 擋不到 Fast Open 送出的連線；``net.mptcp.enabled`` 在沒編 MPTCP 的核心會讓容器
    起不來（Owner 的 WSL2 核心實測 exit 127），只能由 Owner 依核心自己加在覆寫設定."""
    services = _services(rel)
    assert services["touka-proxy"].get("sysctls") == {"net.ipv4.tcp_fastopen": 0}
    assert not [name for name, svc in services.items() if "mptcp" in str(svc.get("sysctls"))]


def test_all_profiles_use_the_same_state_dir():
    assert len({_state_dir(rel) for rel in COMPOSES}) == 1


def test_dev_init_fixes_only_the_volume_root_owner_and_mode():
    init = _services("docker-compose.yml")["touka-init"]
    state = str(_state_dir("docker-compose.yml"))
    assert ("volume", VOLUME, state) in _mounts(init)
    segments = [seg.strip() for seg in init["command"].split(";")]
    for command in ("chown 1001:1001", "chmod 700"):
        pattern = rf"{command} {re.escape(state)}( \|\| true)?"
        assert any(re.fullmatch(pattern, segment) for segment in segments), command
    assert not [s for s in segments if state in s and "-R" in s.split()]


def test_image_ships_a_read_only_root_owned_kernel_copy_besides_copy_all():
    copy_all = _line_index(r"^COPY --chown=appuser:appuser \. \.$")
    match = re.search(r"(?m)^COPY (.*)ToukaProxy/src/sandbox_kernel/ (\S+)/$", DOCKERFILE)
    assert match and match[1] == "", "kernel COPY 不能帶 --chown 等旗標（要 root 擁有）"
    kernel = PurePosixPath(match[2])
    assert _line_index(re.escape(match[0])) > copy_all
    assert not kernel.is_relative_to(INSTALL_ROOT)
    assert re.search(rf"(?m)^ENV {KERNEL_ENV}={re.escape(str(kernel))}$", DOCKERFILE)
    assert re.search(rf"(?m)^RUN chmod -R a=rX {re.escape(str(kernel))}$", DOCKERFILE)
    assert len(re.findall(r"(?m)^COPY\b(?!.*--from).*\s\.\s+\./?$", DOCKERFILE)) == 1


def test_image_precreates_the_state_dir_for_appuser():
    state = _state_dir("docker-compose.yml")
    pattern = rf"install -d -o appuser -g appuser -m 0700 {re.escape(str(state))}(\s|$)"
    assert re.search(pattern, DOCKERFILE)


def test_install_root_keeps_no_dev_or_public_config_files():
    """九個公開設定檔名與開發用設定檔：不是被 .dockerignore 擋掉，就是 uv sync 後刪掉。"""
    removed = re.search(r"(?m)^RUN rm -f (.+)$", DOCKERFILE)
    assert removed
    assert _line_index(re.escape(removed[0])) > _line_index(r"^\s+uv sync --locked")
    assert _line_index(re.escape(removed[0])) > _line_index(r"^COPY --chown=appuser:appuser \. \.$")
    deleted = {PurePosixPath(p) for p in removed[1].split()}
    names = DEV_CONFIGS | launcher.spec.PUBLIC_CONFIG_NAMES
    shipped = {
        name
        for name in names
        if (ROOT / name).exists()
        and _finite_context_included(Path(name), IGNORE_RULES)
        and INSTALL_ROOT / name not in deleted
    }
    assert shipped == set()
    assert deleted <= {INSTALL_ROOT / name for name in names}
