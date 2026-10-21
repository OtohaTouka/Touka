"""R-2c 修正（ADR-0246 底線）：受保護根目錄、F7 繞法、Windows 路徑正規化。

- 受保護根目錄由 SSOT 動態算出（app data、workspace、repo 根、``.env`` 所在目錄）：寫入、
  刪除、搬移、覆寫、``git -C``、解壓縮碰到根目錄底下，或根目錄本身／上層，資料算 F1、
  其他算 F4；遞迴讀取（grep -r、find、zip -r、tar -c、cp -r…）碰到放金鑰的目錄或它的
  上層算 F2。測試把根目錄 monkeypatch 到 tmp（仿 compose 佈局），不寫死絕對路徑。
- F7：前綴指令連同參數跳過；find 的 -delete／-exec rm 算刪除；經管線交給刪除指令時沿用
  左邊的目標；powershell／pwsh 沒帶 -File 時後面整段當指令。
- Windows 正規化：結尾的點與空白、NTFS 資料串流、裝置路徑前綴；心跳 config.json 算 F2。
- 不放寬：常見開發指令照常放行。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from src.config import resolve_env_file_path
from src.models.identity import AgentIdentity
from src.models.policy import PolicyContext
from src.models.safety import AuthPoliciesConfig
from src.services import resource_manager as rm_module
from src.utils import hard_floor
from src.utils.hard_floor import classify_hard_floor, mounts_separate

_REPO = Path(__file__).resolve().parents[2]
_EXAMPLE = _REPO / "ToukaProxy" / "config" / "auth_policies.json.example"
_CONFIG = AuthPoliciesConfig.model_validate(json.loads(_EXAMPLE.read_text(encoding="utf-8")))
_ACTOR = AgentIdentity.for_role(agent_id="FloorRootsProbe", role="Owner", is_human=False)


@dataclass(frozen=True)
class _Layout:
    base: Path
    app: Path
    data: Path
    workspace: Path
    host_root: Path

    def fill(self, template: str) -> str:
        return template.format(
            base=self.base,
            app=self.app,
            data=self.data,
            ws=self.workspace,
            host=self.host_root,
        )


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Layout:
    """仿 compose 佈局：repo 根＝app，底下有 data、workspace、host_root（.env）、ToukaProxy。"""
    app = tmp_path / "app"
    lay = _Layout(tmp_path, app, app / "data", app / "workspace", app / "host_root")
    monkeypatch.setattr(hard_floor, "_REPO_ROOT", app)
    monkeypatch.setattr(hard_floor.resource_manager, "get_workspace_mount", lambda: lay.workspace)
    monkeypatch.setattr(hard_floor.resource_manager, "get_container_mount", lambda: None)
    monkeypatch.setattr(hard_floor.resource_manager, "get_base_path", lambda: str(lay.data))
    monkeypatch.setattr(hard_floor, "get_config_dir", lambda: app / "ToukaProxy" / "config")
    monkeypatch.setattr(hard_floor, "resolve_env_file_path", lambda: lay.host_root / ".env")
    return lay


def _floor(command: str) -> str | None:
    return classify_hard_floor("terminal_execute", command)


def _yolo(command: str):
    return _CONFIG.evaluate(
        PolicyContext(
            actor=_ACTOR,
            resource=command,
            action="terminal_execute",
            environment={"auth_level": "YOLO", "isolation": "landlock"},
        )
    )


# ── 1. 受保護根目錄（reviewer ① 第 1 點的繞法，路徑換成 tmp）──────────────────────

_ROOT_BYPASSES = [
    ("rm -r -f {ws}", "F4"),
    ("rm -fr {app}/ToukaProxy", "F4"),
    ("mv {ws} {base}/gone", "F4"),
    ("cp -r payload/. {app}/ToukaProxy/", "F4"),
    ("tar -xf p.tar -C {app}/ToukaProxy", "F4"),
    ("Remove-Item -Recurse -Force {app}\\ToukaProxy", "F4"),
    ("git -C {ws} reset --hard HEAD~50", "F4"),
    ("git -C {app}/ToukaProxy checkout -- src", "F4"),
    ("grep -rh TOUKA_AUTH_SECRET {host}", "F2"),
    ("zip -r {base}/all.zip {ws}", "F4"),  # ws 是 repo 子資料夾，不含 .env
    ("{ws}; rm -rf {ws}", "F4"),  # 開頭的程式只在它那一段不算目標
    ("echo ok; > {app}/ToukaProxy/main.py", "F4"),
]


@pytest.mark.parametrize(("template", "category"), _ROOT_BYPASSES)
def test_writes_and_recursive_reads_under_protected_roots_are_floor(layout, template, category):
    command = layout.fill(template)
    assert _floor(command) == category
    decision = _yolo(command)
    assert decision.effect in {"HUMAN_ONLY", "DENY"}
    assert decision.metadata["hard_floor"] == category


@pytest.mark.parametrize(
    ("template", "category"),
    [
        ("rm -rf {base}", "F4"),  # 所有根目錄的上層
        ("Move-Item {base} {base}-old", "F4"),
        ("grep -r KEY {app}", "F2"),  # .env 所在目錄的上層
        ("find {base} -name *.json", "F2"),
        ("tar -czf out.tgz {app}", "F2"),
        ("cp -r {base} copy", "F2"),
        ("grep -rn KEY ../..", "F2"),  # 往上跳出工作目錄的遞迴讀取
    ],
)
def test_ancestors_of_protected_roots_are_floor(layout, template, category):
    assert _floor(layout.fill(template)) == category


def test_data_root_outside_repo_is_f1_inside_and_above(layout, monkeypatch):
    sanctuary = layout.base / "outer" / "sanctuary"
    monkeypatch.setattr(hard_floor.resource_manager, "get_base_path", lambda: str(sanctuary))
    assert _floor(f"cp evil.db {sanctuary}/memory.db") == "F1"
    assert _floor(f"mv {sanctuary} {layout.base}/stolen") == "F1"
    assert _floor(f"rm -rf {layout.base / 'outer'}") == "F1"


@pytest.mark.parametrize(
    "template",
    [
        "cp build/app.js {base}/elsewhere/app.js",
        "echo hi > {base}/elsewhere/out.txt",
        "ls -la {app}",
        "ls -ltr {ws}",
        "cat {app}/README.md",
        "grep -rn TODO {base}/elsewhere",
        "git -C {ws} status",
        "git -C {ws} log --oneline",
        '"{app}/.venv/Scripts/python.exe" -c "print(1)" > out.txt',
        "{app}/.venv/bin/node build.js --out dist/",
    ],
)
def test_reads_and_unrelated_writes_stay_open(layout, template):
    assert _floor(layout.fill(template)) is None


def test_real_roots_come_from_the_ssot_without_patching():
    """沒有 monkeypatch：repo 根由 ``Path(__file__)`` 推出、.env 所在目錄由 SSOT 解析。"""
    assert _floor(f"rm -rf {_REPO / 'ToukaProxy'}") == "F4"
    assert _floor(f"git -C {_REPO} checkout -- .") == "F4"
    assert _floor(f"grep -r SECRET {resolve_env_file_path().parent}") == "F2"


# ── 工作區：甲（受保護根目錄本身、底下、上層）與乙（使用者工作區只擋刪除、搬走、清空）──


def _set_workspace(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setattr(hard_floor.resource_manager, "get_workspace_mount", lambda: path)


def test_workspace_equal_to_repo_root_keeps_full_protection(layout, monkeypatch):
    layout.app.mkdir()
    _set_workspace(monkeypatch, layout.app)
    for template, _category in _ROOT_BYPASSES:  # 類別可能 F2／F4 互換，都要擋
        command = layout.fill(template)
        assert _floor(command) is not None, command
        assert _yolo(command).effect in {"HUMAN_ONLY", "DENY"}, command
    assert _floor(layout.fill("echo x > {app}/notes.txt")) == "F4"


def test_workspace_without_touka_only_guards_itself(layout, monkeypatch):
    ws = layout.base / "projects"
    (ws / "src").mkdir(parents=True)
    _set_workspace(monkeypatch, ws)
    for command in (
        f"echo x > {ws}/notes.txt",
        f"cp -r src {ws}/backup",
        f"rm -rf {ws}/src",
        f"grep -rn TODO {ws}",
        f"git -C {ws}/src checkout -- .",
    ):
        assert _floor(command) is None, command
    for command in (f"rm -rf {ws}", f"mv {ws} {ws}-old", f"Remove-Item -Recurse -Force {ws}"):
        assert _floor(command) == "F4", command


def test_workspace_above_the_repo_guards_itself_and_the_repo(layout, monkeypatch):
    (layout.app / "ToukaProxy").mkdir(parents=True)
    (layout.base / "notes").mkdir()
    _set_workspace(monkeypatch, layout.base)
    assert _floor(layout.fill("echo x > {base}/notes/a.txt")) is None
    assert _floor(layout.fill("grep -rn TODO {base}/notes")) is None
    assert _floor(layout.fill("rm -rf {base}")) == "F4"
    assert _floor(layout.fill("mv {base} {base}-old")) == "F4"
    assert _floor(layout.fill("echo x > {app}/ToukaProxy/main.py")) == "F4"


def test_touka_lookalike_folder_is_still_a_user_workspace(layout, monkeypatch):
    """另一份 Touka 的檔案（.env、ToukaProxy/）不是這個實例的受保護根目錄：照乙處理。"""
    ws = layout.base / "deploy"
    (ws / "ToukaProxy").mkdir(parents=True)
    (ws / ".env").write_text("", encoding="utf-8")
    _set_workspace(monkeypatch, ws)
    assert _floor(f"echo x > {ws}/notes.txt") is None
    assert _floor(f"rm -rf {ws}") == "F4"
    assert _floor(f"cat {ws}/.env") == "F2"  # 檔名規則照舊


# ── 2. F7 繞法（reviewer ① 第 2 點）───────────────────────────────────────────

_F7_BYPASSES = [
    "nice -n 19 rm -fr /",
    "timeout 5 rm -rf ~",
    "env -i rm -fr /",
    "nice -n 5 shutdown -h now",
    "timeout 9 docker compose down",
    "find / -delete",
    "echo / | xargs rm -rf",
    "Get-ChildItem C:\\ -Recurse | Remove-Item -Force",
    "powershell Remove-Item -Recurse -Force C:\\",
    # 同一類的延伸寫法
    "sudo -u root rm -rf /",
    "stdbuf -oL nohup ionice -c 3 rm -rf ~",
    "timeout -s KILL 5 rm -rf /",
    "xargs -I {} rm -rf /",
    "find / -exec rm -rf {} +",
    "find ~ -type f -delete",
    "echo / | rm -rf",
    "pwsh -NoProfile -ExecutionPolicy Bypass Stop-Computer",
    "busybox rm -rf /",
    "/usr/bin/sudo -E taskset -c 0 rm -rf ~",
]


@pytest.mark.parametrize("command", _F7_BYPASSES)
def test_f7_bypasses_are_caught_in_yolo(command: str):
    assert _floor(command) == "F7"
    decision = _yolo(command)
    assert decision.effect in {"HUMAN_ONLY", "DENY"}
    assert decision.metadata["hard_floor"] == "F7"


def test_find_delete_under_protected_root_is_a_write(layout):
    assert _floor("find ToukaMemory -name *.py -delete") == "F4"
    assert _floor(layout.fill("find {ws}/src -name *.py -delete")) == "F4"
    assert _floor(layout.fill("find {ws}/src -exec shred {{}} +")) == "F4"


def test_powershell_with_file_script_is_not_expanded():
    """帶 -File 是跑腳本檔，後面的字是腳本參數，不當指令（先寫檔再執行仍是已知限制）。"""
    assert _floor("powershell -NoProfile -File build.ps1 shutdown") is None


# ── 5. Windows 路徑正規化與心跳 config.json ──────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "type .env.",
        "type .env::$DATA",
        "type users.json:secret",
        "Get-Content ToukaProxy\\config\\connections.json. ",
        "cat ../../../ToukaHeartbeat/config.json",
        "type D:\\Touka\\ToukaHeartbeat\\config\\config.json",
    ],
)
def test_windows_normalised_secret_names_are_f2(command: str):
    assert _floor(command) == "F2"


@pytest.mark.parametrize(
    ("template", "category"),
    [
        ("grep -r KEY {host}.", "F2"),
        ("grep -r KEY {host}::$DATA", "F2"),
        ("grep -r KEY {app}. ", "F2"),
        ("grep -r token {app}/ToukaHeartbeat", "F2"),
        ("Remove-Item -Recurse \\\\?\\{app}\\ToukaProxy", "F4"),
    ],
)
def test_windows_normalised_roots(layout, template, category):
    assert _floor(layout.fill(template)) == category


@pytest.mark.parametrize(
    "command",
    ["cat config.json", "cat ToukaHeartbeat/config.json.example", "type .env.example."],
)
def test_lookalike_names_are_not_secret(command: str):
    assert _floor(command) is None


# ── 不放寬：常見開發指令照常放行 ─────────────────────────────────────────────────

_DEV_COMMANDS = [
    "python -m pytest tests/ -q",
    "pytest tests/",
    "npm run build",
    "npm test",
    "npx vitest run",
    "git status",
    "git add .",
    'git commit -m "update data loader"',
    "git checkout -b feature/x",
    "git log --oneline | head -5",
    "pip install -r requirements.txt",
    "mkdir -p out && cp a.txt out/",
    "cp -r src/ backup/",
    "cat README.md",
    "ls -la",
    "ls -ltr",
    "node server.js",
    "python script.py",
    "rm -rf build/",
    "rm -rf node_modules",
    "del /s /q build",
    "echo hello > out.txt",
    "type .env.example",
    "docker ps",
    "docker logs touka-proxy",
    "curl https://example.com -o page.html",
    "Get-ChildItem -Recurse",
    "Get-ChildItem -Recurse | Select-String TODO",
    "grep -rn TODO src/",
    'find . -name "*.pyc" -delete',
    "ls | xargs wc -l",
    "find . -name '*.txt' | xargs grep -l shutdown",
    "nice -n 10 python train.py",
    "timeout 60 npm test",
    "tar -czf out.tgz build/",
    "powershell -NoProfile -File build.ps1",
    "pwsh -Command Get-ChildItem",
]


@pytest.mark.parametrize("command", _DEV_COMMANDS)
def test_common_dev_commands_are_not_floor(command: str):
    assert _floor(command) is None
    assert _yolo(command).effect == "ALLOW"


# ── Docker：工作區掛在 repo 根底下（/app/workspace），mountinfo 證明不重疊才豁免 ──────

_PROJECT_COMMANDS = [
    "git -C {ws} checkout -b x",
    "git -C {ws} stash",
    "cp -r out {ws}/dist",
    "cp -r {ws}/src ./work",
    "cd {ws} && python manage.py test",
    "python {ws}/run.py",
    "echo x > {ws}/notes.txt",
    "cd {ws} && rm -rf node_modules",
]
_EMPTYING = [  # 清空或刪除工作區本身的變形寫法（乙）
    "rm -rf {ws}/*",
    "mv {ws}/* {base}/x",
    "Remove-Item {ws}\\* -Recurse",
    "rsync -a --delete empty/ {ws}/",
    "robocopy empty {ws} /MIR",
    "robocopy empty {ws} /PURGE",
    "git -C {ws} clean -fdx",
    "python -c \"shutil.rmtree('{ws}')\"",
    "python -c \"import shutil; shutil.rmtree('{ws}')\"",
    "rm -rf {ws}/**",
    "del /s /q {ws}\\*.*",
    "rm -f {ws}/*.*",
    "node -e \"require('fs').rmSync('{ws}', {{recursive: true}})\"",
    "perl -MFile::Path=remove_tree -e \"remove_tree('{ws}')\"",
    "npx rimraf {ws}",
]
_INSIDE_OK = [  # 只動工作區裡面：照常放行
    "rsync -a --delete out/ {ws}/dist/",
    "robocopy src {ws}/dist /E",
    "git -C {ws} status",
    "python {ws}/manage.py migrate",
]


def _mountinfo(*entries: tuple[str, str, Path]) -> str:
    """假的 /proc/self/mountinfo；每項（裝置, 宿主來源, 容器掛載點）。"""
    return "\n".join(
        f"{i} 1 {dev} {root} {str(point).replace(chr(92), '/')} rw - ext4 /dev/x rw"
        for i, (dev, root, point) in enumerate(entries, start=20)
    )


def _compose_mounts(layout: _Layout, ws_root: str, ws_dev: str = "8:1") -> str:
    """compose：宿主 repo 在 /Touka；data、host_root、設定目錄都從它掛進來。"""
    return _mountinfo(
        (ws_dev, ws_root, layout.workspace),
        ("8:1", "/Touka", layout.host_root),
        ("8:1", "/Touka/data", layout.data),
        ("8:1", "/Touka/ToukaProxy/config", layout.app / "ToukaProxy" / "config"),
    )


def _compose_project(
    layout: _Layout, monkeypatch: pytest.MonkeyPatch, ws_root: str = "/Projects/foo"
) -> Path:
    """仿 compose：repo 根 app 底下有原始碼與工作區掛載點；預設掛的是使用者自己的專案。"""
    (layout.app / "ToukaProxy" / "src").mkdir(parents=True)
    (layout.workspace / "src").mkdir(parents=True)
    _set_workspace(monkeypatch, layout.workspace)
    monkeypatch.setattr(
        hard_floor.resource_manager, "get_container_mount", lambda: layout.workspace
    )
    text = _compose_mounts(layout, ws_root)
    monkeypatch.setattr(hard_floor, "_read_mountinfo", lambda: text)
    return layout.workspace


@pytest.mark.parametrize("template", _PROJECT_COMMANDS + _INSIDE_OK)
def test_proven_separate_mount_runs_project_commands(layout, monkeypatch, template):
    _compose_project(layout, monkeypatch)
    command = layout.fill(template)
    assert _floor(command) is None
    assert _yolo(command).effect == "ALLOW"


@pytest.mark.parametrize(
    "template",
    [
        "rm -rf {ws}",
        "mv {ws} {base}/gone",
        "Remove-Item -Recurse -Force {ws}",
        "echo {ws} | xargs rm -rf",
        "find {ws} -delete",
        "rm -rf {app}/ToukaProxy",
        "echo x > {app}/ToukaProxy/src/main.py",
        "cp evil.db {data}/x.db",
        *_EMPTYING,
    ],
)
def test_proven_separate_mount_still_guards_itself_and_touka(layout, monkeypatch, template):
    _compose_project(layout, monkeypatch)
    assert _floor(layout.fill(template)) in {"F1", "F4"}


@pytest.mark.parametrize("template", _EMPTYING)
def test_user_workspace_outside_docker_guards_emptying_variants(layout, monkeypatch, template):
    ws = layout.base / "projects"
    ws.mkdir()
    _set_workspace(monkeypatch, ws)
    assert _floor(template.format(ws=ws, base=layout.base)) == "F4"


@pytest.mark.parametrize("ws_root", ["/", "/Touka", "/Touka/scripts"])
def test_mount_overlapping_touka_keeps_full_protection(layout, monkeypatch, ws_root):
    """宿主端工作區是 Touka 的上層、就是它，或在它裡面：不豁免，整片保護。"""
    _compose_project(layout, monkeypatch, ws_root)
    for template, _category in _ROOT_BYPASSES:
        assert _floor(layout.fill(template)) is not None, template
    assert _floor(layout.fill("echo x > {ws}/notes.txt")) == "F4"
    assert _floor(layout.fill("grep -rn KEY {ws}")) == "F2"


_MP = "/ctr"  # 中性前綴：mounts_separate 收的是參數，不需要真的部署路徑
_ANCHORS = (f"{_MP}/data", f"{_MP}/host_root", f"{_MP}/ToukaProxy/config")
# Docker named volume 的宿主來源（中性的資料根目錄；佈局 …/volumes/<名稱>/_data）
_VOLUME = "/docker-data/volumes/touka_proxy-config/_data"


def _pure_mounts(
    ws_root: str,
    ws_dev: str = "8:1",
    ws_point: str = f"{_MP}/workspace",
    root_dev: str = "0:40",
    *,
    anchor_dev: str = "8:1",
    config_root: str = "/Touka/ToukaProxy/config",
    config_dev: str | None = None,
) -> str:
    return "\n".join(
        [
            f"22 1 {root_dev} / / rw - overlay overlay rw",
            f"30 22 {ws_dev} {ws_root} {ws_point} rw master:1 - ext4 /dev/sda1 rw",
            f"31 22 {anchor_dev} /Touka {_MP}/host_root rw - ext4 /dev/sda1 rw",
            f"32 22 {anchor_dev} /Touka/data {_MP}/data rw - ext4 /dev/sda1 rw",
            f"33 22 {config_dev or anchor_dev} {config_root} {_MP}/ToukaProxy/config rw - x y rw",
        ]
    )


@pytest.mark.parametrize(
    ("label", "text", "separate"),
    [
        ("a 工作區是宿主根、同裝置", _pure_mounts("/"), False),
        ("b 工作區是別的專案、同裝置", _pure_mounts("/Projects/foo"), True),
        ("c 解析失敗", "garbage line", False),
        ("c 有一行缺分隔欄", _pure_mounts("/Projects/foo") + "\n40 22 8:2 /x /mnt/x rw", False),
        ("c 找不到工作區掛載項", _pure_mounts("/Projects/foo", ws_point="/elsewhere"), False),
        (
            "c 沒有獨立掛載項（容器根在實體裝置）",
            _pure_mounts("/Projects/foo", ws_point="/elsewhere", root_dev="253:0"),
            False,
        ),
        ("c 讀不到（空字串）", "", False),
        ("d 兩顆實體磁碟（假設不重疊）", _pure_mounts("/", ws_dev="8:17"), True),
        ("d2 不同的 major 0 裝置（FUSE、9p）", _pure_mounts("/", ws_dev="0:99"), False),
        ("g 同裝置、大小寫不同", _pure_mounts("/touka"), False),
        (
            "h Touka 以 rbind 子掛載出現在工作區底下",
            _pure_mounts("/Projects/foo", ws_dev="8:17")
            + f"\n34 30 8:1 /Touka {_MP}/workspace/Touka rw - ext4 /dev/sda1 rw",
            False,
        ),
        ("e 工作區就是 host_root", _pure_mounts("/Touka"), False),
        (
            "i 設定目錄是實體裝置上的 named volume、工作區在 9p",
            _pure_mounts("/Projects/foo", ws_dev="0:60", anchor_dev="0:60", config_root=_VOLUME),
            True,
        ),
        (
            "j 同樣佈局但 volume 在 major 0",
            _pure_mounts(
                "/Projects/foo",
                ws_dev="0:60",
                anchor_dev="0:60",
                config_root=_VOLUME,
                config_dev="0:61",
            ),
            False,
        ),
        ("f 工作區在宿主 repo 裡", _pure_mounts("/Touka/scripts"), False),
        (
            "k volume 佈局少了 _data",
            _pure_mounts(
                "/Projects/foo",
                ws_dev="0:60",
                anchor_dev="0:60",
                config_root="/docker-data/volumes/x/data",
                config_dev="8:1",
            ),
            False,
        ),
        (
            "k _data 位置不對",
            _pure_mounts(
                "/Projects/foo",
                ws_dev="0:60",
                anchor_dev="0:60",
                config_root="/docker-data/volumes/x/y/_data",
                config_dev="8:1",
            ),
            False,
        ),
        (
            "l 工作區本身是 volume、錨點在 major 0",
            _pure_mounts("/docker-data/volumes/w/_data", ws_dev="8:1", anchor_dev="0:60"),
            False,
        ),
        (
            "m 同裝置：工作區是 Docker 資料根，只有 volume 在它底下",
            _pure_mounts(
                "/var/lib/docker",
                anchor_dev="8:1",
                config_root="/var/lib/docker/volumes/touka_proxy-config/_data",
            ),
            False,
        ),
    ],
)
def test_mounts_separate_proof(label, text, separate):
    assert mounts_separate(text, f"{_MP}/workspace", _ANCHORS) is separate, label


def test_mounts_separate_decodes_escaped_paths():
    text = _pure_mounts("/My\\040Projects", ws_point=f"{_MP}/work\\040space")
    assert mounts_separate(text, f"{_MP}/work space", _ANCHORS) is True


# ── 「工作區設定 × 動作」對照表：每一格寫死期望（底線類別或 None）──────────────────

_ACTIONS = {
    "write_in": "echo x > {ws}/notes.txt",
    "copy_onto": "cp -r out {ws}",
    "delete": "rm -rf {ws}",
    "move": "mv {ws} {base}/moved",
    "empty": "rsync -a --delete empty/ {ws}/",
    "read_tree": "grep -rn KEY {ws}",
    "delete_repo": "rm -rf {repo}",
    "read_env": "cat {env}/.env",
    "touka_write": "echo x > {ws}/Touka/pyproject.toml",
    "touka_delete": "rm -rf {ws}/Touka",
}
# 欄：寫入工作區裡、複製到工作區本身、刪除工作區、搬走工作區、清空工作區、遞迴讀工作區、
#     刪除 repo 根、讀 .env、寫工作區裡那份 Touka、刪工作區裡那份 Touka（"-"＝這列不適用）
_COLUMNS = tuple(_ACTIONS)
# fmt: off
_MATRIX = {
    #                    write  copy   delete move   empty  tree   repo   .env   t-write t-del
    "repo_root":        ("F4",  "F4",  "F4",  "F4",  "F4",  "F2",  "F4",  "F2",  "-",    "-"),
    "repo_parent":      (None,  "F4",  "F4",  "F4",  "F4",  "F2",  "F4",  "F2",  "F4",   "F4"),
    "repo_sibling":     (None,  None,  "F4",  "F4",  "F4",  None,  "F4",  "F2",  "-",    "-"),
    "repo_subfolder":   ("F4",  "F4",  "F4",  "F4",  "F4",  None,  "F4",  "F2",  "-",    "-"),
    "empty":            (None,  None,  None,  None,  None,  None,  "F4",  "F2",  "-",    "-"),
    "docker_separate":  (None,  None,  "F4",  "F4",  "F4",  None,  "F4",  "F2",  "-",    "-"),
    "docker_has_touka": ("F4",  "F4",  "F4",  "F4",  "F4",  "F2",  "F4",  "F2",  "F4",   "F4"),
    "docker_unknown":   ("F4",  "F4",  "F4",  "F4",  "F4",  "F2",  "F4",  "F2",  "-",    "-"),
    # 第 6 輪：證明更嚴（大小寫、major 0 裝置、子掛載）與不在 Docker 的別名工作區
    "docker_case_differs":   ("F4", "F4", "F4", "F4", "F4", "F2", "F4", "F2", "F4", "F4"),
    "docker_fuse_per_bind":  ("F4", "F4", "F4", "F4", "F4", "F2", "F4", "F2", "-",  "-"),
    "docker_rbind_touka":    ("F4", "F4", "F4", "F4", "F4", "F2", "F4", "F2", "F4", "F4"),
    "docker_cross_drive":    ("F4", "F4", "F4", "F4", "F4", "F2", "F4", "F2", "-",  "-"),
    "docker_two_disks":      (None, None, "F4", "F4", "F4", None, "F4", "F2", "-",  "-"),
    "docker_case_apart":     (None, None, "F4", "F4", "F4", None, "F4", "F2", "-",  "-"),
    "alias_to_repo":         ("F4", "F4", "F4", "F4", "F4", "F2", "F4", "F2", "-",  "-"),
    # U2c-R1c：設定目錄是 VM 磁碟上的 Docker named volume（ghcr 加 Docker Desktop）
    "docker_ghcr_desktop":       (None, None, "F4", "F4", "F4", None, "F4", "F2", "-", "-"),
    "docker_bind_not_volume":    ("F4", "F4", "F4", "F4", "F4", "F2", "F4", "F2", "-", "-"),
    "docker_volume_same_device": ("F4", "F4", "F4", "F4", "F4", "F2", "F4", "F2", "-", "-"),
    "docker_fuse_view":          ("F4", "F4", "F4", "F4", "F4", "F2", "F4", "F2", "-", "-"),
    "docker_volume_on_major0":   ("F4", "F4", "F4", "F4", "F4", "F2", "F4", "F2", "-", "-"),
    "docker_volume_under_ws":    ("F4", "F4", "F4", "F4", "F4", "F2", "F4", "F2", "-", "-"),
}
# fmt: on
_CELLS = [
    (setting, action, expected)
    for setting, row in _MATRIX.items()
    for action, expected in zip(_COLUMNS, row, strict=True)
    if expected != "-"
]


def _matrix_row(setting: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """建一列的佈局；accessor 用真的（只改它讀的設定），不寫死路徑。"""
    manager = hard_floor.resource_manager
    docker = setting.startswith("docker")
    repo = tmp_path / ("app" if docker else "Touka")
    data, config = repo / "data", repo / "ToukaProxy" / "config"
    env = repo / "host_root" if docker else repo  # 不在 Docker：.env 在 repo 根
    for folder in (data, env, config):
        folder.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(hard_floor, "_REPO_ROOT", repo)
    monkeypatch.setattr(manager, "get_base_path", lambda: str(data))
    monkeypatch.setattr(hard_floor, "get_config_dir", lambda: config)
    monkeypatch.setattr(hard_floor, "resolve_env_file_path", lambda: env / ".env")
    monkeypatch.setattr(manager, "_workspace_root_cached", None)
    monkeypatch.setattr(manager, "_is_docker", docker)
    if docker:
        ws = repo / "workspace"
        monkeypatch.setattr(manager, "_container_mount", ws)

        def touka(dev: str, root: str) -> list[tuple[str, str, Path]]:
            return [
                (dev, root, env),
                (dev, f"{root}/data", data),
                (dev, f"{root}/ToukaProxy/config", config),
            ]

        entries = {  # 工作區掛載項＋Touka 的三個錨點（docker_unknown：讀不到 mountinfo）
            "docker_separate": [("8:1", "/Projects/foo", ws), *touka("8:1", "/Touka")],
            "docker_has_touka": [("8:1", "/", ws), *touka("8:1", "/Touka")],
            "docker_case_differs": [("8:1", "/projects", ws), *touka("8:1", "/Projects/Touka")],
            "docker_fuse_per_bind": [
                ("0:51", "/", ws),
                ("0:52", "/", env),
                ("0:53", "/", data),
                ("0:54", "/", config),
            ],
            "docker_rbind_touka": [
                ("8:17", "/Projects/foo", ws),
                *touka("8:1", "/Touka"),
                ("8:1", "/Touka", ws / "Touka"),  # rbind 帶進來的子掛載
            ],
            "docker_cross_drive": [("0:61", "/Projects/foo", ws), *touka("0:62", "/Touka")],
            "docker_two_disks": [("8:17", "/Projects/foo", ws), *touka("8:1", "/Touka")],
            "docker_case_apart": [("8:1", "/Projects/Foo", ws), *touka("8:1", "/Touka")],
            # U2c-R1c：ghcr compose 跑在 Docker Desktop（設定目錄是 VM 磁碟上的 named volume）
            "docker_ghcr_desktop": [
                ("0:60", "/Projects/foo", ws),
                ("0:60", "/Touka", env),
                ("0:60", "/Touka/data", data),
                ("8:1", _VOLUME, config),
            ],
            "docker_bind_not_volume": [  # (a) 設定目錄是一般 bind，不是 volume
                ("0:60", "/Projects/foo", ws),
                ("0:60", "/Touka", env),
                ("0:60", "/Touka/data", data),
                ("8:1", "/Touka/ToukaProxy/config", config),
            ],
            "docker_volume_same_device": [  # (b) 同裝置照舊比 root：工作區是上層
                ("8:1", "/", ws),
                ("8:1", "/Touka", env),
                ("8:1", "/Touka/data", data),
                ("8:1", _VOLUME, config),
            ],
            "docker_fuse_view": [  # (c) volume 證明分開，但 host_root 證明不了
                ("0:70", "/", ws),
                ("0:71", "/Touka", env),
                ("0:71", "/Touka/data", data),
                ("8:1", _VOLUME, config),
            ],
            "docker_volume_on_major0": [  # (d) volume 佈局出現在 major 0 掛載項
                ("0:60", "/Projects/foo", ws),
                ("0:60", "/Touka", env),
                ("0:60", "/Touka/data", data),
                ("0:61", _VOLUME, config),
            ],
            "docker_volume_under_ws": [  # 同裝置：工作區是 Docker 資料根，只有 volume 在它底下
                ("8:1", "/var/lib/docker", ws),
                ("8:1", "/Touka", env),
                ("8:1", "/Touka/data", data),
                ("8:1", "/var/lib/docker/volumes/touka_proxy-config/_data", config),
            ],
        }.get(setting)
        text = _mountinfo(*entries) if entries else ""
        monkeypatch.setattr(hard_floor, "_read_mountinfo", lambda: text)
        return {"ws": ws, "repo": repo, "env": env, "base": tmp_path}
    if setting == "alias_to_repo":  # 工作區是指向 repo 的 symlink／junction
        ws = tmp_path / "link"
        try:
            ws.symlink_to(repo, target_is_directory=True)
        except OSError:  # Windows 沒有建 symlink 的權限：模擬 resolve 的結果
            real_resolve = Path.resolve
            monkeypatch.setattr(
                Path,
                "resolve",
                lambda self, strict=False: repo if self == ws else real_resolve(self, strict),
            )
        monkeypatch.setattr(rm_module, "HOST_WORKSPACE_ROOT", str(ws))
        return {"ws": ws, "repo": repo, "env": env, "base": tmp_path}
    ws = {
        "repo_root": repo,
        "repo_parent": tmp_path,
        "repo_sibling": tmp_path / "Projects",
        "repo_subfolder": repo / "scripts",
    }.get(setting)
    monkeypatch.setattr(rm_module, "HOST_WORKSPACE_ROOT", "" if ws is None else str(ws))
    if ws is None:  # 空值：沒有工作區；拿目前資料夾來檢查「不被當成工作區」
        ws = tmp_path / "scratch"
        ws.mkdir()
        monkeypatch.chdir(ws)
    return {"ws": ws, "repo": repo, "env": env, "base": tmp_path}


@pytest.mark.parametrize(("setting", "action", "expected"), _CELLS)
def test_workspace_setting_by_action_matrix(tmp_path, monkeypatch, setting, action, expected):
    places = _matrix_row(setting, tmp_path, monkeypatch)
    assert _floor(_ACTIONS[action].format(**places)) == expected


def test_workspace_mount_comes_from_resource_manager(monkeypatch, tmp_path):
    manager = rm_module.resource_manager
    monkeypatch.setattr(manager, "_is_docker", True)
    assert manager.get_workspace_mount() == Path("/app/workspace")  # noqa: PATH_LITERAL_SSOT
    assert manager.get_container_mount() == Path("/app/workspace")  # noqa: PATH_LITERAL_SSOT
    monkeypatch.setattr(manager, "_is_docker", False)
    monkeypatch.setattr(manager, "_workspace_root_cached", None)
    monkeypatch.setattr(rm_module, "HOST_WORKSPACE_ROOT", str(tmp_path))
    assert manager.get_workspace_mount() == tmp_path
    assert manager.get_container_mount() is None


@pytest.mark.parametrize(
    ("cached", "host"), [(None, ""), (None, "   "), (Path(""), "x"), (None, "rel")]
)
def test_empty_or_relative_workspace_setting_is_not_a_workspace(monkeypatch, cached, host):
    manager = rm_module.resource_manager
    monkeypatch.setattr(manager, "_is_docker", False)
    monkeypatch.setattr(manager, "_workspace_root_cached", cached)
    monkeypatch.setattr(rm_module, "HOST_WORKSPACE_ROOT", host)
    assert manager.get_workspace_mount() is None


def _real_workspace_accessors(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    manager = hard_floor.resource_manager
    monkeypatch.delattr(manager, "get_workspace_mount")  # 拿掉 fixture 的替身，用真的 accessor
    monkeypatch.delattr(manager, "get_container_mount")
    monkeypatch.setattr(manager, "_is_docker", False)
    monkeypatch.setattr(manager, "_workspace_root_cached", None)
    monkeypatch.setattr(rm_module, "HOST_WORKSPACE_ROOT", host)


def test_empty_workspace_setting_never_guesses_the_current_folder(layout, monkeypatch):
    (layout.app / "ToukaProxy").mkdir(parents=True)
    scratch = layout.base / "scratch"
    scratch.mkdir()
    _real_workspace_accessors(monkeypatch, "")
    monkeypatch.chdir(layout.app / "ToukaProxy")
    assert _floor(layout.fill("echo x > {app}/ToukaProxy/pyproject.toml")) == "F4"
    monkeypatch.chdir(scratch)
    assert _floor(f"rm -rf {scratch}") is None  # 沒設工作區：不把目前資料夾當工作區


def test_non_docker_workspace_inside_the_repo_is_fully_protected(layout, monkeypatch):
    scripts = layout.app / "scripts"
    scripts.mkdir(parents=True)
    _set_workspace(monkeypatch, scripts)  # 不在 Docker：在 repo 底下，依甲整片保護
    assert _floor(layout.fill("echo x > {app}/scripts/a.py")) == "F4"
    assert _floor(layout.fill("cp -r out {app}/scripts/dist")) == "F4"


@pytest.mark.parametrize("template", ["rm -rf {ws}/ToukaProxy", "echo x > {ws}/docker-compose.yml"])
def test_alias_workspace_into_the_repo_is_rule_a(tmp_path, monkeypatch, template):
    places = _matrix_row("alias_to_repo", tmp_path, monkeypatch)
    assert _floor(template.format(**places)) == "F4"


def test_workspace_resolve_failure_counts_as_overlap(layout, monkeypatch):
    ws = layout.base / "projects"
    ws.mkdir()
    _set_workspace(monkeypatch, ws)

    def _fail(*_args, **_kwargs):
        raise OSError

    with monkeypatch.context() as patch:
        patch.setattr(Path, "resolve", _fail)
        result = _floor(f"echo x > {ws}/notes.txt")
    assert result == "F4"
