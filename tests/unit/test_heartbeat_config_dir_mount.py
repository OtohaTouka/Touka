"""ADR-0150：心跳設定改掛『目錄』而非單檔 bind-mount（避開『目錄疊檔案』啟動崩潰）。

背景（症狀根因）：
    Docker 對「缺席」的單檔 bind-mount 來源會自動建成一個空「目錄」，再把這個目錄掛到
    容器內是「檔案」的 ``/app/config.json`` 上 → 型別衝突（runc: ``not a directory``）。
    這個崩潰發生在容器啟動「之前」，因此 ``touka-init`` 的 up-only 播種（depends_on 只在
    ``up`` 生效、不在 ``restart``／daemon 重啟）擋不住——正是使用者重啟時遇到的錯。

修法（本測試釘死）：
    heartbeat 掛「設定目錄」並以 HEARTBEAT_CONFIG_FILE 指向其中的 config.json。
    開發 compose 保留原初始化；GHCR 依 ADR-0221 要求既存目錄與 long bind false，
    initializer 僅調整 metadata。Windows C1 的來源自動建立能力缺口另有原始 FAIL，
    本來源測試不宣稱掛載恆成功或正式 startup 無內容寫入。

覆蓋：
    1. 兩份 compose 的 heartbeat「設定掛載」目標為『目錄』(/app/config)，且脆弱單檔
       (:/app/config.json) 已完全消失。
    2. heartbeat 服務設定 HEARTBEAT_CONFIG_FILE=/app/config/config.json。
    3. app 端：awaken._resolve_config_file 依 HEARTBEAT_CONFIG_FILE 覆寫（未設定回退
       BASE_DIR/config.json）；StorageService 於（模擬掛載的）空目錄內自癒生成 config.json。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ToukaHeartbeat import permission_init
from ToukaHeartbeat.awaken import _resolve_config_file
from ToukaHeartbeat.storage_service import StorageService

ROOT = Path(__file__).resolve().parents[2]
COMPOSES = [ROOT / "docker-compose.yml", ROOT / "docker-compose.ghcr.yml"]


def _heartbeat_service(compose_path: Path) -> dict:
    data = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    return data["services"]["touka-heartbeat"]


def _str_list(value: object) -> list[str]:
    return [v for v in (value or []) if isinstance(v, str)]


# ── 1 & 2：compose 掛載形狀（目錄，非單檔）＋ 設定檔路徑注入 ──────────────────────


@pytest.mark.parametrize("compose_path", COMPOSES, ids=lambda p: p.name)
def test_heartbeat_config_mounts_a_directory_not_a_file(compose_path: Path):
    """heartbeat 設定掛載目標必須是『目錄』/app/config，脆弱單檔 :/app/config.json 須消失。

    以「後綴」比對容器端目標（而非 split(':')），因來源可能是 ``${VAR:-default}`` 含冒號。
    """
    raw = _heartbeat_service(compose_path).get("volumes")
    assert isinstance(raw, list)
    targets = [
        value.rsplit(":", 1)[-1] if isinstance(value, str) else value["target"] for value in raw
    ]

    assert "/app/config" in targets, (
        f"{compose_path.name}: heartbeat 設定應掛『目錄』/app/config（ADR-0150）；"
        f"實得 targets={targets!r}"
    )
    assert "/app/config.json" not in targets, (
        f"{compose_path.name}: 仍存在脆弱的單檔掛載 :/app/config.json（缺席即崩潰）"
    )


@pytest.mark.parametrize("compose_path", COMPOSES, ids=lambda p: p.name)
def test_touka_init_command_has_no_inline_comments(compose_path: Path):
    """touka-init 的 sh -c 指令（YAML 折疊標量 `>`）內嚴禁 `#` 註解。

    折疊標量會把 6-space 註解行與後續指令折成同一行 → `#` 之後整段（含 if/then）變成 shell
    註解，而更深縮排的 cp 與 fi 保留換行 → 產生懸空 `fi` → busybox sh 報 `unexpected fi`
    導致 touka-init exit 2、依賴它的 heartbeat 永不啟動。此測試釘死該回歸（實際踩過）。
    以 yaml 解析後的『折疊字串』（＝容器實跑的 shell）檢查，正是 shell 所見。
    """
    data = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    init_command = data["services"]["touka-init"].get("command", "")
    parts = [init_command] if isinstance(init_command, str) else init_command
    assert isinstance(parts, list) and parts and all(isinstance(part, str) for part in parts)
    assert all("#" not in part for part in parts), (
        f"{compose_path.name}: touka-init 指令含 `#`——折疊標量會折進指令列造成 shell 語法錯誤；"
        f"註解請移到 command 之外（YAML 註解）。"
    )


@pytest.mark.parametrize("compose_path", [COMPOSES[0]], ids=lambda p: p.name)
def test_touka_init_migrates_and_seeds_config(compose_path: Path):
    """開發 compose 保留『遷移舊單檔』與『.example 播種』兩條路徑。

    ADR-0150 對抗式審查 #4：遷移是資料保全關鍵路徑，須有回歸鎖點。init 應：
      - mkdir 設定目錄；
      - 遷移舊單檔（cp 舊 config.json → 目錄內 config/config.json）保留使用者排程；
      - 否則從 config.json.example 播種（同步建檔＝CI fresh-volume 閘可確定性 test -f）。
    以結構斷言鎖住 shell 分支；行為面另有本機 Docker 端到端實測（見 ADR「端到端驗證」）。
    """
    data = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    cmd = data["services"]["touka-init"].get("command", "")
    assert "mkdir" in cmd, f"{compose_path.name}: init 應 mkdir 設定目錄"
    assert "config.json.example" in cmd, (
        f"{compose_path.name}: init 應含 .example 播種分支（新目錄無 config 時）"
    )
    assert "config/config.json" in cmd, (
        f"{compose_path.name}: init 應把設定寫入目錄內 config/config.json（遷移／播種目標）"
    )


def test_ghcr_init_is_permission_only_with_shared_existing_config_directory():
    """GHCR 只調權限；四掛載、設定來源與 false 都由同一產品 SSOT 接合。"""
    data = yaml.safe_load(COMPOSES[1].read_text(encoding="utf-8"))
    services = data["services"]
    init = services["touka-init"]
    helper = permission_init
    assert init["command"] == [
        "python",
        "-I",
        "-B",
        Path(helper.__file__).name,
        "--profile",
        helper.PROFILE,
        "--expected-uid",
        "1001",
        "--expected-gid",
        "1001",
    ]
    mounts = init["volumes"]
    assert len(mounts) == len(helper.LAYOUT)
    assert tuple(Path(mount["target"]).name for mount in mounts) == helper.LAYOUT
    config = mounts[-1]
    heartbeat = services["touka-heartbeat"]["volumes"]
    assert len(heartbeat) == 1
    assert config["type"] == heartbeat[0]["type"] == "bind"
    assert (
        config["source"] == heartbeat[0]["source"] == "${HEARTBEAT_CONFIG_DIR:-./heartbeat-config}"
    )
    assert config["bind"] == heartbeat[0]["bind"] == {"create_host_path": False}
    assert heartbeat[0]["target"] == "/app/config"


@pytest.mark.parametrize("compose_path", COMPOSES, ids=lambda p: p.name)
def test_heartbeat_sets_config_file_env(compose_path: Path):
    """heartbeat 必須以 HEARTBEAT_CONFIG_FILE 指向掛載目錄內的 config.json。"""
    env = _str_list(_heartbeat_service(compose_path).get("environment"))
    assert any(
        e.replace(" ", "").startswith("HEARTBEAT_CONFIG_FILE=/app/config/config.json") for e in env
    ), (
        f"{compose_path.name}: heartbeat 應設 HEARTBEAT_CONFIG_FILE=/app/config/config.json；"
        f"env={env!r}"
    )


# ── 3：app 端解析與自癒 ──────────────────────────────────────────────────────────


def test_resolve_config_file_env_override(tmp_path):
    """HEARTBEAT_CONFIG_FILE 覆寫時 awaken 指向該檔（Docker 佈署＝掛載目錄內的 config.json）。"""
    target = tmp_path / "config" / "config.json"
    assert _resolve_config_file(tmp_path, str(target)) == target


def test_resolve_config_file_defaults_to_base_dir(tmp_path):
    """未設定／空白 HEARTBEAT_CONFIG_FILE 時回退 base_dir/config.json。"""
    assert _resolve_config_file(tmp_path, "") == tmp_path / "config.json"
    assert _resolve_config_file(tmp_path, "   ") == tmp_path / "config.json"


def test_storage_self_heals_inside_mounted_dir(tmp_path):
    """模擬 Docker 掛載的（可能剛被自動建立的）空設定目錄：

    StorageService 於其中自癒生成 config.json——這正是『掛目錄』修法能穩定啟動的關鍵：
    容器起得來 → 應用層補檔，無須依賴 up-only 的 touka-init 播種。
    """
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "config.json"

    storage = StorageService(cfg_file, env_token="")
    result = storage.load_config()

    assert cfg_file.exists(), "StorageService 應於掛載目錄內生成 config.json"
    assert len(result.get("heartbeat_token", "")) >= 16, "自癒 config 必含有效 token"
    assert result.get("schedules") == [], "自癒 config 的 schedules 應為空陣列"


# ── G12（守衛涵蓋缺口）：CONFIG_FILE 解析刻意留在 live-mount 的 awaken.py 直讀 env ──────
def test_config_file_resolution_stays_in_awaken_not_baked_config():
    """HEARTBEAT_CONFIG_FILE 的解析須在 awaken.py 直讀 os.environ，不移進烤進映像的 config.py。

    ADR-0150 明文『刻意』：dev compose 只 live-mount awaken.py、config.py 已烤進映像；改走
    config.py 會讓熱修需 --build 才生效。功能測試仍全綠（函式仍可運作），故需源碼守衛鎖此
    dev『up -d 免 --build 即生效』的支點。
    """
    hb = ROOT / "ToukaHeartbeat"
    awaken_src = (hb / "awaken.py").read_text(encoding="utf-8")
    config_src = (hb / "config.py").read_text(encoding="utf-8")

    assert (
        'os.environ.get("HEARTBEAT_CONFIG_FILE"' in awaken_src
        or "os.environ.get('HEARTBEAT_CONFIG_FILE'" in awaken_src
    ), "awaken.py 未直讀 HEARTBEAT_CONFIG_FILE env（dev 免-rebuild 熱修支點；ADR-0150/G12）"
    assert "HEARTBEAT_CONFIG_FILE" not in config_src, (
        "HEARTBEAT_CONFIG_FILE 解析被移進烤進映像的 config.py＝dev 免-rebuild 熱修靜默失效"
    )
