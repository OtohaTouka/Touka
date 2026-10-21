from pathlib import Path

import pytest
import yaml

from ToukaHeartbeat import permission_init


def test_docker_compose_permissions_and_init():
    """驗證 docker-compose.yml 包含了對 Volume 權限衝突的 Init Container 修復配置。"""
    compose_path = Path(__file__).parent.parent.parent / "docker-compose.yml"
    assert compose_path.exists(), "docker-compose.yml 檔案應存在"

    with open(compose_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    services = config.get("services", {})

    # 1. 驗證是否定義了 touka-init 服務
    assert "touka-init" in services, "應定義 touka-init 服務以修正掛載目錄的權限"

    init_service = services["touka-init"]

    # 2. 驗證 touka-init 服務配置
    assert init_service.get("user") == "root", "touka-init 必須以 root 身份執行以進行 chown"

    # 3. 驗證 touka-proxy 服務是否依賴於 touka-init 成功結束
    assert "touka-proxy" in services, "應定義 touka-proxy 服務"
    proxy_service = services["touka-proxy"]

    depends_on = proxy_service.get("depends_on", {})
    assert "touka-init" in depends_on, "touka-proxy 應依賴於 touka-init"
    assert depends_on["touka-init"].get("condition") == "service_completed_successfully", (
        "touka-proxy 對 touka-init 的依賴條件必須為 service_completed_successfully"
    )

    # 4. 驗證 touka-init 的 volumes 包含對 heartbeat 的掛載
    init_volumes = init_service.get("volumes", [])
    assert any("ToukaHeartbeat" in vol for vol in init_volumes), (
        "touka-init 的 volumes 應掛載 ToukaHeartbeat 目錄"
    )

    # 5. 驗證 touka-init 的 command 備妥 heartbeat 設定『目錄』並遷移舊單檔（ADR-0150）。
    #    改掛目錄後不再從 .example 播種單檔（config.json 由 heartbeat 的 StorageService 於
    #    目錄內自癒生成）；init 僅需 mkdir 目錄＋權限＋遷移既有舊單檔以保留使用者排程。
    init_command = init_service.get("command", "")
    assert "app/heartbeat/config" in init_command, (
        "touka-init command 應備妥 heartbeat 設定目錄 /app/heartbeat/config（ADR-0150 改掛目錄）"
    )
    assert "mkdir" in init_command, (
        "touka-init command 應 mkdir 設定目錄（缺席時 Docker 自動建成目錄＝正確型別）"
    )
    assert "app/heartbeat/config/config.json" in init_command, (
        "touka-init command 應遷移舊單檔到目錄內 config/config.json（保留使用者排程）"
    )

    # 6. 驗證 touka-heartbeat 依賴於 touka-init
    assert "touka-heartbeat" in services, "應定義 touka-heartbeat 服務"
    heartbeat_service = services["touka-heartbeat"]
    heartbeat_depends = heartbeat_service.get("depends_on", {})
    assert "touka-init" in heartbeat_depends, "touka-heartbeat 應依賴於 touka-init"
    # depends_on 可以是 list 或 dict。在原 yml 中是 dict，所以：
    assert heartbeat_depends["touka-init"].get("condition") == "service_completed_successfully", (
        "touka-heartbeat 對 touka-init 的依賴條件必須為 service_completed_successfully"
    )


def test_heartbeat_token_not_overridden_in_compose():
    """touka-heartbeat 不得在 environment 硬覆寫 HEARTBEAT_TOKEN（禁令 15 密鑰同步）.

    compose environment 的 ``${VAR}`` 為「解析期」取值，會把 HEARTBEAT_TOKEN 鎖成空字串
    並壓過 ``env_file``，使 proxy 自動生成寫回 .env 的 token 無法同步到 heartbeat 容器。
    故此值必須由 ``env_file: .env`` 取得。此測試只驗設定不覆寫；
    depends_on 不保證同次 Compose 解析會重讀稍後生成的 token，首裝須分 invocation。
    """
    compose_path = Path(__file__).parent.parent.parent / "docker-compose.yml"
    with open(compose_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    heartbeat = config["services"]["touka-heartbeat"]

    # token 的實際來源：env_file 必須包含 .env
    env_file = heartbeat.get("env_file", [])
    env_file = [env_file] if isinstance(env_file, str) else env_file
    assert any(".env" in e for e in env_file), "touka-heartbeat 應以 env_file: .env 取得密鑰"

    # environment 不得再出現 HEARTBEAT_TOKEN= 覆寫（list 或 dict 兩種寫法皆檢查）
    environment = heartbeat.get("environment", [])
    if isinstance(environment, dict):
        assert "HEARTBEAT_TOKEN" not in environment, (
            "touka-heartbeat environment 不得覆寫 HEARTBEAT_TOKEN（會壓過 env_file 同步）"
        )
    else:
        assert not any(str(e).startswith("HEARTBEAT_TOKEN=") for e in environment), (
            "touka-heartbeat environment 不得覆寫 HEARTBEAT_TOKEN（會壓過 env_file 同步）"
        )


# ── E23（ADR-0038/ADR-0185）：init chown 涵蓋面＋pull compose 純度 ────────────────

_ROOT = Path(__file__).parent.parent.parent


def _load_yaml(rel: str) -> dict:
    with open(_ROOT / rel, encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_formal_local_profile_enforces_hardened_settings():
    config = _load_yaml("docker-compose.ghcr.yml")
    for name in ("touka-proxy", "touka-heartbeat", "touka-chat"):
        service = config["services"][name]
        assert service["user"] == "1001:1001"
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert all(port.startswith("127.0.0.1:") for port in service["ports"])
    init = config["services"]["touka-init"]
    assert init["user"] == "root" and init["network_mode"] == "none"
    assert set(init["cap_add"]) == {"CHOWN", "FOWNER", "DAC_OVERRIDE"}


_SOURCE_FRAGMENTS = ("ToukaProxy", "ToukaMemory", "ToukaDispatch", "ToukaChat", "awaken")


def _bind_source(volume) -> str | None:
    """回傳 bind mount 的 host 來源；named volume 回 None。"""
    if isinstance(volume, dict):
        return volume["source"] if volume.get("type") == "bind" else None
    source = volume[: volume.find(":/")]
    return source if source.startswith((".", "/", "$")) else None


def test_pull_compose_uses_only_ghcr_images_without_source_mounts():
    """pull 使用者沒有 repo：全服務走 ghcr.io 映像、無 build:、無源碼 bind mount。"""
    for name, service in _load_yaml("docker-compose.ghcr.yml")["services"].items():
        assert service["image"].startswith("ghcr.io/"), name
        assert "build" not in service, name
        for volume in service.get("volumes", []):
            source = _bind_source(volume)
            assert source is None or not any(f in source for f in _SOURCE_FRAGMENTS), (
                name,
                volume,
            )


def _chown_segments(cmd: str) -> str:
    """萃取 command 中的 chown 指令段（以 `;` 切段；YAML 折疊標量已把換行折成空白）。

    綁定「chown 涵蓋面」必須看 chown 段本身——整段 substring 會被 chmod 行的同名
    路徑遮蔽（刪 chown 仍全綠＝測試自己假綠）。
    """
    return ";".join(seg for seg in cmd.split(";") if "chown" in seg)


def test_dev_init_chowns_all_service_writable_mounts():
    """dev compose 的 touka-init 必須 chown 所有服務要寫的掛載（E23：先前零守衛）。

    刪 chown → uid-1001 寫不進 named volume（hf-cache 於模型下載時）crash；
    bind mount 在部分主機 uid 不合時同病。變異刪 chown/漏列目錄 → 紅。
    """
    config = _load_yaml("docker-compose.yml")
    cmd = config["services"]["touka-init"].get("command", "")
    chown = _chown_segments(cmd)
    assert "chown -R 1001:1001" in chown, "touka-init 必須 chown 1001:1001（appuser）"
    for target in ("/app/config", "/app/data", "/app/hf_cache", "/app/heartbeat/config"):
        assert target in chown, f"touka-init chown 必須涵蓋 {target}（服務以 1001 寫入的掛載）"


def test_ghcr_init_chowns_all_service_writable_mounts():
    """Pull init 只以受審 helper 修正四個 role 的 metadata，先於產品服務。"""
    config = _load_yaml("docker-compose.ghcr.yml")
    services = config["services"]
    command = services["touka-init"]["command"]
    assert command[:4] == ["python", "-I", "-B", "permission_init.py"]
    assert (
        tuple(Path(mount["target"]).name for mount in services["touka-init"]["volumes"])
        == permission_init.LAYOUT
    )
    # named volumes 宣告存在（首建 root-owned＝chown 的存在理由）
    assert set(config.get("volumes", {})) >= {"hf-cache", "proxy-config"}
    # proxy 必須等 init 成功結束（chown 先於服務啟動）
    dep = services["touka-proxy"]["depends_on"]["touka-init"]
    assert dep.get("condition") == "service_completed_successfully"


# Q42: accepted metadata-only init contract. No Docker or true deployment bindings.
def test_q42_init_uses_direct_permission_only_argv():
    config = _load_yaml("docker-compose.ghcr.yml")
    assert config["services"]["touka-init"]["command"] == [
        "python",
        "-I",
        "-B",
        "permission_init.py",
        "--profile",
        "local-permissions-v1",
        "--expected-uid",
        "1001",
        "--expected-gid",
        "1001",
    ]


def test_q42_init_closed_four_long_mount_roles():
    config = _load_yaml("docker-compose.ghcr.yml")
    mounts = config["services"]["touka-init"]["volumes"]
    assert len(mounts) == 4 and all(isinstance(mount, dict) for mount in mounts)
    assert {mount["target"].rsplit("/", 1)[-1] for mount in mounts} == {
        "data",
        "hf_cache",
        "proxy_config",
        "heartbeat_config",
    }
    assert all(mount["source"] != "./" for mount in mounts)


@pytest.mark.parametrize("service", ["touka-init", "touka-proxy", "touka-heartbeat"])
def test_q42_permission_bind_sources_do_not_auto_create(service):
    config = _load_yaml("docker-compose.ghcr.yml")
    sources = {"${SANCTUARY_DATA_PATH:-./data}", "${HEARTBEAT_CONFIG_DIR:-./heartbeat-config}"}
    mounts = [
        mount
        for mount in config["services"][service]["volumes"]
        if isinstance(mount, dict) and mount.get("source") in sources
    ]
    assert len(mounts) == (2 if service == "touka-init" else 1)
    assert all(
        mount["type"] == "bind" and mount["bind"] == {"create_host_path": False} for mount in mounts
    )


def test_q42_init_named_volumes_disable_implicit_copy():
    config = _load_yaml("docker-compose.ghcr.yml")
    mounts = [
        mount
        for mount in config["services"]["touka-init"]["volumes"]
        if isinstance(mount, dict) and mount.get("type") == "volume"
    ]
    assert {mount["source"] for mount in mounts} == {"hf-cache", "proxy-config"}
    assert all(mount.get("volume") == {"nocopy": True} for mount in mounts)
