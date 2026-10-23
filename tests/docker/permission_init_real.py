"""touka-init（``permission_init.py``）在真的 Linux 核心上的檢查（只用標準函式庫，ADR-0279）.

用法::

    python tests/docker/permission_init_real.py --image <proxy 映像> [--keep]

在主機上用 docker 跑。測試資料只放在這個腳本自己建的 volume（``touka-pinit-test-*``）裡，由映像內的
root 建立；init 用和 GHCR compose 相同的權限執行（root、只留 CHOWN／FOWNER／DAC_OVERRIDE、不連網），
每個角色是同一個 volume 的 subpath 各掛一次，所以多連結檔能指到角色以外、同一個檔案系統上的檔。

三種情況，任一不符就 exit 1：

- ``pass``：照常通過；略過的項目（Hugging Face 快取形狀的符號連結、指到外面的連結、FIFO、socket、
  多連結檔、setuid 檔、工作區的 ``node_modules``）前後不變，外面那個 root 的檔沒被碰；該改的改成
  1001，只需改擁有者的 setgid 資料夾改完 setgid 還在。
- ``setgid_needs_bits``：setgid 資料夾還要補擁有者權限位元，核心會清掉 setgid，照舊失敗。
- ``root_xattr``：角色根目錄帶延伸屬性，照舊失敗。

不加 ``--keep`` 時結束後刪掉建立的 volume；容器都是 ``--rm``。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import NoReturn

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "ToukaHeartbeat" / "permission_init.py"
ROLES = ("data", "hf_cache", "proxy_config", "heartbeat_config")
CAPS = ("CHOWN", "FOWNER", "DAC_OVERRIDE")
HELPER_ARGS = [
    "--profile", "local-permissions-v1", "--expected-uid", "1001", "--expected-gid", "1001"
]  # fmt: skip
PREFIX = "touka-pinit-test"

COPY_HELPER = "import sys; open('/v/tool/permission_init.py', 'wb').write(sys.stdin.buffer.read())"

SETUP = r"""
import os, socket, sys

case = sys.argv[1]
os.umask(0)


def folder(path, mode=0o755, uid=1000):
    if not os.path.isdir(path):
        os.mkdir(path)
    os.chown(path, uid, uid)
    os.chmod(path, mode)


def file(path, mode=0o644, uid=1000):
    with open(path, "w") as handle:
        handle.write("x")
    os.chown(path, uid, uid)
    os.chmod(path, mode)


def link(target, path):
    os.symlink(target, path)
    os.lchown(path, 1000, 1000)


folder("/v/tool", 0o755, 0)
for role in ("data", "hf_cache", "proxy_config", "heartbeat_config"):
    folder("/v/tool/" + role, 0o755, 0)
    folder("/v/" + role)
folder("/v/outside", 0o700, 0)
file("/v/outside/secret", 0o600, 0)

if case == "pass":
    file("/v/data/ordinary")
    alice = "/v/data/personal_drive/Alice"
    modules = alice + "/project/node_modules"
    for path in ("/v/data/personal_drive", alice, alice + "/missions", alice + "/project"):
        folder(path)
    for path in (modules, modules + "/pkg", modules + "/.bin"):
        folder(path)
    file(alice + "/missions/board.json")
    file(modules + "/pkg/index.js")
    link("../pkg/index.js", modules + "/.bin/tool")
    folder("/v/data/mcp_servers")
    os.mkfifo("/v/data/fifo", 0o644)
    os.chown("/v/data/fifo", 1000, 1000)
    server = socket.socket(socket.AF_UNIX)
    server.bind("/v/data/sock")
    server.close()
    os.chown("/v/data/sock", 1000, 1000)
    os.link("/v/outside/secret", "/v/data/hard")
    file("/v/data/setuid", 0o4755)
    folder("/v/data/shared", 0o2770)
    for path in ("models--x", "models--x/blobs", "models--x/snapshots", "models--x/snapshots/main"):
        folder("/v/hf_cache/" + path)
    file("/v/hf_cache/models--x/blobs/abc")
    link("../../blobs/abc", "/v/hf_cache/models--x/snapshots/main/config.json")
    link("/v/outside/secret", "/v/hf_cache/link-out")
    file("/v/proxy_config/settings.json")
elif case == "setgid_needs_bits":
    folder("/v/data/needbits", 0o2070)
elif case == "root_xattr":
    os.setxattr("/v/hf_cache", "user.touka", b"1")
else:
    raise SystemExit("unknown case " + case)
"""

OBSERVE = r"""
import json, os, stat

found = {}
for top, folders, files in os.walk("/v"):
    for name in folders + files:
        path = os.path.join(top, name)
        info = os.lstat(path)
        found[os.path.relpath(path, "/v")] = [info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)]
print(json.dumps(found))
"""

CHANGED = {
    "data": 0o755,
    "data/ordinary": 0o644,
    "data/personal_drive": 0o755,
    "data/personal_drive/Alice/missions/board.json": 0o644,
    "data/mcp_servers": 0o755,
    "data/shared": 0o2770,
    "hf_cache": 0o755,
    "hf_cache/models--x/blobs/abc": 0o644,
    "proxy_config/settings.json": 0o644,
    "heartbeat_config": 0o755,
}
UNCHANGED = (
    "data/personal_drive/Alice/project/node_modules",
    "data/personal_drive/Alice/project/node_modules/pkg/index.js",
    "data/personal_drive/Alice/project/node_modules/.bin/tool",
    "data/fifo",
    "data/sock",
    "data/hard",
    "data/setuid",
    "hf_cache/models--x/snapshots/main/config.json",
    "hf_cache/link-out",
    "outside/secret",
)
PASS_SKIPPED = {
    "symlink": 2,
    "fifo": 1,
    "socket": 1,
    "device": 0,
    "hardlink": 1,
    "setid_file": 1,
    "xattr": 0,
    "tool_dir": 1,
}
EXPECTED_FAILURE = {
    "setgid_needs_bits": "PARTIAL_PERMISSION_FAILURE",
    "root_xattr": "UNSUPPORTED_METADATA",
}


def _fail(message: str) -> NoReturn:
    sys.stderr.write(f"{message}\n")
    raise SystemExit(1)


def _docker(args: list[str], stdin: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    docker = shutil.which("docker") or _fail("docker is not on PATH")
    return subprocess.run([docker, *args], input=stdin, capture_output=True, check=False)


def _as_root(image: str, volume: str, *command: str, stdin: bytes = b"") -> bytes:
    root = ["run", "--rm", "-i", "--user", "0", "--network", "none", "-v", f"{volume}:/v"]
    run = _docker([*root, image, *command], stdin)
    if run.returncode != 0:
        _fail("helper container failed: " + run.stderr.decode(errors="replace")[-400:])
    return run.stdout


def _run_init(image: str, volume: str) -> dict[str, object]:
    mounts = ["--mount", f"type=volume,src={volume},dst=/pinit,volume-subpath=tool,readonly"]
    for role in ROLES:
        mounts += ["--mount", f"type=volume,src={volume},dst=/pinit/{role},volume-subpath={role}"]
    caps = [f"--cap-add={cap}" for cap in CAPS]
    isolation = ["--user", "0", "--network", "none", "--cap-drop", "ALL", *caps]
    command = ["python", "-I", "-B", "/pinit/permission_init.py", *HELPER_ARGS]
    run = _docker(["run", "--rm", *isolation, *mounts, image, *command])
    lines = run.stdout.decode(errors="replace").strip().splitlines()
    if not lines:
        _fail("touka-init printed nothing: " + run.stderr.decode(errors="replace")[-400:])
    return json.loads(lines[-1])


def _observe(image: str, volume: str) -> dict[str, list[int]]:
    return json.loads(_as_root(image, volume, "python", "-I", "-c", OBSERVE))


def _check(case: str, image: str, volume: str) -> list[str]:
    _as_root(image, volume, "python", "-I", "-c", SETUP, case)
    _as_root(image, volume, "python", "-I", "-c", COPY_HELPER, stdin=HELPER.read_bytes())
    before = _observe(image, volume)
    result = _run_init(image, volume)
    after = _observe(image, volume)
    problems = []
    if case == "pass":
        if result.get("status") != "PASS":
            problems.append(f"expected PASS, got {result}")
        if result.get("skipped") != PASS_SKIPPED:
            problems.append(f"skipped counts {result.get('skipped')} != {PASS_SKIPPED}")
        for path, mode in CHANGED.items():
            if after.get(path) != [1001, 1001, mode]:
                problems.append(f"{path}: {after.get(path)} != [1001, 1001, {oct(mode)}]")
        for path in UNCHANGED:
            if path not in before or after.get(path) != before[path]:
                problems.append(f"{path} changed: {before.get(path)} -> {after.get(path)}")
    else:
        expected = EXPECTED_FAILURE[case]
        if result.get("status") != "FAIL" or result.get("reason") != expected:
            problems.append(f"expected FAIL/{expected}, got {result}")
        if after.get("outside/secret") != before.get("outside/secret"):
            problems.append("outside/secret changed")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", required=True)
    parser.add_argument("--keep", action="store_true", help="keep the test volumes for inspection")
    args = parser.parse_args()
    created = []
    failed = False
    try:
        for case in ("pass", *EXPECTED_FAILURE):
            volume = f"{PREFIX}-{case.replace('_', '-')}-{uuid.uuid4().hex[:8]}"
            if _docker(["volume", "create", volume]).returncode != 0:
                _fail(f"cannot create volume {volume}")
            created.append(volume)
            problems = _check(case, args.image, volume)
            report = {"case": case, "ok": not problems, "problems": problems}
            sys.stdout.write(f"{json.dumps(report)}\n")
            failed = failed or bool(problems)
    finally:
        if args.keep:
            sys.stdout.write(f"{json.dumps({'kept_volumes': created})}\n")
        else:
            for volume in created:
                _docker(["volume", "rm", "-f", volume])
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
