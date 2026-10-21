# ruff: noqa: T201, S603, S607
"""ADR-0117 — Environment Parity Guard（第 30 道守衛，uv workspace 版）。

單一 ``uv.lock`` 世界的「開發 ↔ 部署」一致性不變量（fail-closed、**純讀檔、無網路、秒級**）：

1. **鎖檔新鮮度**：每個 workspace 成員 ``pyproject.toml`` 的 ``[project].dependencies``
   （與 root ``[dependency-groups].dev``）必須與 ``uv.lock`` 內記錄的
   ``requires-dist``／``requires-dev`` **逐項一致（名稱＋規格）**——離線、決定性地擋掉
   「改了 pyproject 卻忘了 ``uv lock``」。（Docker build 的 ``uv sync --locked`` 亦會擋，
   此處把防線前移到 ``guard_all``；取代 ADR-0071 的 ``# lock-input-sha256:`` 戳記制。）
2. **Python 版本一致**：``.python-version`` / root＋成員 pyproject ``requires-python``
   / 各 ``FROM python:`` Dockerfile / ``ruff target-version`` 全部對齊同一個 minor。
3. **釘版政策**：pyproject 依賴（含 dev group）不得含 ``>=``（只允許 ``~=`` / ``==``）。

Checks 1–3 為檔案級、確定性、無 venv 依賴（``guard_all.py`` 即跑這三項，跨機一致）。

**Check 4（opt-in，``--venv`` / ``--venv-path``）— venv-parity advisory**：以 ``uv pip freeze``
唯讀比對開發 venv 與 ``uv.lock`` 釘版（只報「已裝版本 ∉ 鎖檔版本集」；不報未安裝——
per-member 子集安裝與平台 marker 使『未裝』屬正常）。**純資訊：一律 exit 0、不進
``guard_all``、永不影響 build。**

掃描函式皆為純函式、可單元測試（TDD）。Checks 1–3 任一違反即非零退出。
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# workspace 成員：目錄名 → uv.lock 內的套件名
_MEMBERS: dict[str, str] = {
    "ToukaProxy": "toukaproxy",
    "ToukaMemory": "toukamemory",
    "ToukaHeartbeat": "toukaheartbeat",
    "ToukaDispatch": "toukadispatch",
}
_ROOT_MEMBER = "touka"
# 有 Dockerfile 的 Python 服務（ToukaMemory/Dispatch 隨 proxy 單一映像，無獨立 Dockerfile）。
_DOCKER_PY = ("ToukaProxy", "ToukaHeartbeat")
_DEFAULT_PY = "3.12"


def _norm_name(name: str) -> str:
    """PEP 503 正規化套件名（``discord.py`` → ``discord-py``）。"""
    return re.sub(r"[-_.]+", "-", name.strip().lower())


# ── Check 1：鎖檔新鮮度（純函式，pyproject ↔ uv.lock 逐項比對）────────────────
def parse_req(req: str) -> tuple[str, str]:
    """PEP 508 依賴字串 → ``(正規化名, 規格)``。

    ``"pydantic[email]~=2.12.5"`` → ``("pydantic", "~=2.12.5")``（extras 不參與比對——
    uv.lock 的 requires-dist 將 extras 記於獨立欄位）；環境 marker（``;`` 後）截斷。
    """
    s = req.split(";")[0].strip()
    m = re.match(r"^([A-Za-z0-9._-]+)(?:\[[^\]]*\])?\s*(.*)$", s)
    if not m:
        return _norm_name(s), ""
    return _norm_name(m.group(1)), (m.group(2) or "").replace(" ", "")


def pyproject_req_pairs(doc: dict) -> set[tuple[str, str]]:
    """Return base and optional project requirements as normalized pairs."""
    project = doc.get("project", {})
    deps = list(project.get("dependencies", []) or [])
    for group in (project.get("optional-dependencies", {}) or {}).values():
        deps.extend(group or [])
    return {parse_req(r) for r in deps}


def pyproject_dev_pairs(doc: dict) -> set[tuple[str, str]]:
    """Root pyproject dict → ``[dependency-groups].dev`` 的 ``(名, 規格)`` 集合。"""
    deps = doc.get("dependency-groups", {}).get("dev", []) or []
    return {parse_req(r) for r in deps}


def _entry_pairs(entries: list[dict]) -> set[tuple[str, str]]:
    """uv.lock 的 requires-dist / requires-dev 條目 → ``(名, 規格)`` 集合。"""
    return {
        (_norm_name(e.get("name", "")), (e.get("specifier", "") or "").replace(" ", ""))
        for e in entries
    }


def lock_member_pairs(lock: dict, member: str) -> set[tuple[str, str]] | None:
    """Return one virtual or installable-editable workspace member's requirements."""
    for pkg in lock.get("package", []) or []:
        source = pkg.get("source") or {}
        if pkg.get("name") == member and ("virtual" in source or "editable" in source):
            return _entry_pairs((pkg.get("metadata") or {}).get("requires-dist", []) or [])
    return None


def lock_root_dev_pairs(lock: dict) -> set[tuple[str, str]] | None:
    """uv.lock dict → root 成員記錄的 requires-dev(dev group) 集合（找不到回 None）。"""
    for pkg in lock.get("package", []) or []:
        if pkg.get("name") == _ROOT_MEMBER and "virtual" in (pkg.get("source") or {}):
            meta = pkg.get("metadata") or {}
            return _entry_pairs((meta.get("requires-dev") or {}).get("dev", []) or [])
    return None


def diff_pairs(declared: set[tuple[str, str]], locked: set[tuple[str, str]]) -> list[str]:
    """比對宣告 vs 鎖檔記錄，回傳人類可讀差異（空 = 一致）。"""
    out: list[str] = []
    for name, spec in sorted(declared - locked):
        out.append(f"pyproject 宣告 {name}{spec}，但 uv.lock 未記錄（或規格不同）")
    for name, spec in sorted(locked - declared):
        out.append(f"uv.lock 記錄 {name}{spec}，但 pyproject 已無此宣告")
    return out


# ── Check 2：Python 版本一致（純函式）──────────────────────────
def norm_pyver(value: str) -> str | None:
    """把 ``3.12`` / ``3.12.12`` / ``==3.12.*`` 正規化為 ``3.12``。"""
    m = re.search(r"(\d+)\.(\d+)", value)
    return f"{m.group(1)}.{m.group(2)}" if m else None


def py_from_target(target: str) -> str | None:
    """``py312`` → ``3.12``；``py314`` → ``3.14``。"""
    m = re.fullmatch(r"py(\d)(\d+)", target.strip().strip("\"'"))
    return f"{m.group(1)}.{m.group(2)}" if m else None


def py_from_dockerfile(text: str) -> str | None:
    """Return one minor from tagged Python stages or explicit pinned apk add commands."""
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    logical = re.sub(r"\\\n", " ", "\n".join(lines))
    versions = {
        f"{match.group(1)}.{match.group(2)}"
        for match in re.finditer(
            r"^\s*FROM\s+(?:--platform=\S+\s+)?python:(\d+)\.(\d+)(?=[.\s@-]|$)",
            logical,
            re.MULTILINE | re.IGNORECASE,
        )
    }
    for line in logical.splitlines():
        instruction = re.match(r"^\s*RUN\s+(.+)$", line, re.IGNORECASE)
        if instruction is None:
            continue
        # Keep quoted punctuation as a word; echo ';' must not create an apk command.
        lexer = shlex.shlex(instruction.group(1), posix=False, punctuation_chars=";&|")
        lexer.whitespace_split = True
        try:
            tokens = list(lexer)
        except ValueError:
            return None
        command: list[str] = []
        for token in [*tokens, ";"]:
            if token not in {";", "&&", "||", "|", "&"}:
                command.append(token)
                continue
            if command and command[-1].endswith("\\"):
                return None
            if command[:2] == ["apk", "add"]:
                for package in command[2:]:
                    if re.match(r"python-\d+\.\d+(?:[=<>~]|$)", package) is None:
                        continue
                    pin = re.fullmatch(r"python-(\d+\.\d+)=(\d+\.\d+)\.\d+-r\d+", package)
                    if pin is None or pin.group(1) != pin.group(2):
                        return None
                    versions.add(pin.group(1))
            command = []
    return next(iter(versions)) if len(versions) == 1 else None


def py_from_requires(text: str) -> str | None:
    """pyproject.toml ``requires-python = "==3.12.*"`` → ``3.12``。"""
    m = re.search(r'requires-python\s*=\s*"([^"]+)"', text)
    return norm_pyver(m.group(1)) if m else None


def py_from_ruff(text: str) -> str | None:
    """ruff.toml ``target-version = "py312"`` → ``3.12``。"""
    m = re.search(r'target-version\s*=\s*"([^"]+)"', text)
    return py_from_target(m.group(1)) if m else None


# ── Check 3：釘版政策（純函式）────────────────────────────────
def scan_ge_reqs(reqs: list[str]) -> list[str]:
    """回傳依賴字串清單中使用 ``>=`` 者（依賴主權：只允許 ``~=`` / ``==``）。"""
    return [r for r in reqs if ">=" in r]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _load_toml(path: Path) -> dict | None:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None


# ── Check 4（opt-in）：venv-parity advisory（唯讀、純比對 + 薄 I/O）──────────
def _installed_map(freeze_text: str) -> dict[str, str]:
    """``uv pip freeze`` 文字 → {正規化名: 版本}（略過 editable/URL/註解）。"""
    out: dict[str, str] = {}
    for line in freeze_text.splitlines():
        s = line.strip()
        if not s or s.startswith(("#", "-e")) or " @ " in s:
            continue
        m = re.match(r"^([A-Za-z0-9._-]+)==([^\s;]+)", s)
        if m:
            out[_norm_name(m.group(1))] = m.group(2)
    return out


def lock_version_map(lock: dict) -> dict[str, set[str]]:
    """Return normalized names and locked versions, excluding virtual members."""
    out: dict[str, set[str]] = {}
    for pkg in lock.get("package", []) or []:
        if "virtual" in (pkg.get("source") or {}):
            continue
        name = _norm_name(pkg.get("name", ""))
        ver = pkg.get("version")
        if name and ver:
            out.setdefault(name, set()).add(ver)
    return out


def venv_parity_drift(freeze_text: str, lockmap: dict[str, set[str]]) -> list[str]:
    """唯讀比對：報「已裝版本 ∉ 鎖檔版本集」。

    不報「未安裝」（per-member 子集安裝與平台 marker 使未裝屬正常）、不報 extras
    （裝了但不在鎖檔的套件，如臨時工具）。
    """
    installed = _installed_map(freeze_text)
    drift: list[str] = []
    for name, iv in sorted(installed.items()):
        locked = lockmap.get(name)
        if locked is not None and iv not in locked:
            drift.append(f"[VENV] {name}：已裝 {iv} ∉ uv.lock 鎖定版本 {sorted(locked)}")
    return drift


def _freeze_text(venv_path: Path | None = None) -> str | None:
    """``uv pip freeze``（唯讀，stdin=DEVNULL）；失敗/無 uv/無 venv 回 None。"""
    cmd = ["uv", "pip", "freeze"]
    if venv_path is not None:
        sub = "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
        cmd += ["--python", str(venv_path / sub)]
    try:
        res = subprocess.run(
            cmd,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout if res.returncode == 0 else None


def venv_advisory(venv_path: Path | None = None) -> list[str]:
    """組裝 venv-parity advisory（唯讀；無法取得 freeze / 無鎖檔時回單行 skip）。"""
    freeze_text = _freeze_text(venv_path)
    if freeze_text is None:
        return ["[VENV] skipped：無法取得 uv pip freeze（uv 未安裝或無 venv）"]
    lock = _load_toml(PROJECT_ROOT / "uv.lock")
    if lock is None:
        return ["[VENV] skipped：無法讀取 uv.lock"]
    return venv_parity_drift(freeze_text, lock_version_map(lock))


def run_all() -> list[str]:
    """執行三項檢查，回傳所有違規訊息。"""
    errors: list[str] = []

    lock = _load_toml(PROJECT_ROOT / "uv.lock")
    if lock is None:
        errors.append("[LOCK] 缺 uv.lock 或無法解析 → 跑 `uv lock`")

    root_doc = _load_toml(PROJECT_ROOT / "pyproject.toml")
    if root_doc is None:
        errors.append("[LOCK] 缺 root pyproject.toml 或無法解析")

    # Check 1：鎖檔新鮮度（pyproject ↔ uv.lock 逐項比對）
    if lock is not None:
        for sp, member in _MEMBERS.items():
            doc = _load_toml(PROJECT_ROOT / sp / "pyproject.toml")
            if doc is None:
                errors.append(f"[LOCK] {sp}：缺 pyproject.toml 或無法解析")
                continue
            locked = lock_member_pairs(lock, member)
            if locked is None:
                errors.append(f"[LOCK] uv.lock 找不到 workspace 成員 '{member}' → 跑 `uv lock`")
                continue
            for msg in diff_pairs(pyproject_req_pairs(doc), locked):
                errors.append(f"[LOCK] {sp}：{msg} → 跑 `uv lock`")
        if root_doc is not None:
            locked_dev = lock_root_dev_pairs(lock)
            if locked_dev is None:
                errors.append(f"[LOCK] uv.lock 找不到 root 成員 '{_ROOT_MEMBER}' → 跑 `uv lock`")
            else:
                for msg in diff_pairs(pyproject_dev_pairs(root_doc), locked_dev):
                    errors.append(f"[LOCK] root dev group：{msg} → 跑 `uv lock`")

    # Check 2：Python 版本一致（以 .python-version 為基準）
    versions: dict[str, str] = {}
    pv = PROJECT_ROOT / ".python-version"
    if pv.exists():
        v = norm_pyver(_read(pv))
        if v:
            versions[".python-version"] = v
    for label, p in [("pyproject:requires-python", PROJECT_ROOT / "pyproject.toml")] + [
        (f"{sp}/pyproject:requires-python", PROJECT_ROOT / sp / "pyproject.toml") for sp in _MEMBERS
    ]:
        if p.exists():
            v = py_from_requires(_read(p))
            if v:
                versions[label] = v
    ruff = PROJECT_ROOT / "ruff.toml"
    if ruff.exists():
        v = py_from_ruff(_read(ruff))
        if v:
            versions["ruff:target-version"] = v
    for sp in _DOCKER_PY:
        df = PROJECT_ROOT / sp / "Dockerfile"
        v = py_from_dockerfile(_read(df))
        if v:
            versions[f"{sp}/Dockerfile"] = v
        else:
            errors.append(f"[PYVER] {sp}/Dockerfile：缺失或無法辨識唯一釘版 Python minor")
    canonical = versions.get(".python-version", _DEFAULT_PY)
    bad = {k: v for k, v in versions.items() if v != canonical}
    if bad:
        errors.append(f"[PYVER] 期望 {canonical}（依 .python-version），不一致： {bad}")

    # Check 3：釘版政策（pyproject 依賴禁 >=）
    for sp in _MEMBERS:
        doc = _load_toml(PROJECT_ROOT / sp / "pyproject.toml")
        if doc is None:
            continue
        deps = doc.get("project", {}).get("dependencies", []) or []
        for line in scan_ge_reqs(deps):
            errors.append(f"[PIN] {sp}/pyproject.toml 禁用 '>='（請用 '~='）： {line}")
    if root_doc is not None:
        dev_deps = root_doc.get("dependency-groups", {}).get("dev", []) or []
        for line in scan_ge_reqs(dev_deps):
            errors.append(f"[PIN] root dev group 禁用 '>='（請用 '~='）： {line}")

    return errors


def main(argv: list[str] | None = None) -> int:
    """執行 Checks 1–3（阻斷、決定 exit）；``--venv`` 時附帶唯讀 advisory（不影響 exit）。"""
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(add_help=True, description="Environment Parity Guard (#30)")
    parser.add_argument("--venv", action="store_true", help="附帶唯讀 venv-parity advisory")
    parser.add_argument("--venv-path", default=None, help="advisory 目標 venv（預設當前 .venv）")
    args, _ = parser.parse_known_args(argv)

    if sys.stdout.encoding != "utf-8" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 60)
    print("Environment Parity Guard (Guard #30)")
    print("=" * 60)
    errors = run_all()
    if not errors:
        print("[OK] 鎖檔新鮮度 / Python 版本 / 釘版政策 三項一致。")
    else:
        print(f"[FAIL] 發現 {len(errors)} 項違規：")
        for e in errors:
            print("  " + e)

    # Check 4（opt-in）：唯讀 venv-parity advisory —— 純資訊，不影響 exit。
    if args.venv or args.venv_path:
        target = Path(args.venv_path) if args.venv_path else None
        print("-" * 60)
        print("[venv-parity advisory]（唯讀；不影響 exit）")
        drift = venv_advisory(target)
        if not drift:
            print("  [OK] 開發 venv 與 uv.lock 釘版一致。")
        else:
            for d in drift:
                print("  " + d)

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
