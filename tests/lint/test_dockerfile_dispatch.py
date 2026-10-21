"""TDD: dispatch 隨 proxy 單一映像出貨的完整性靜態驗證（ADR-0082）.

原契約「ToukaDispatch/Dockerfile COPY 完整性」已隨獨立容器退役；新契約等價保障：
root-context 的 ToukaProxy/Dockerfile 以 COPY . .（禁令 47）烤入整個 repo，
故 dispatch 源碼（server.py/session_manager.py/config/）進映像的充要條件是
「根 .dockerignore 不得排除 ToukaDispatch」——否則容器啟動 spawn 即 ModuleNotFoundError。
同時釘住安全黑名單：.env／活體 config／data 絕不可烤進映像。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROXY_DOCKERFILE = ROOT / "ToukaProxy" / "Dockerfile"
ROOT_DOCKERIGNORE = ROOT / ".dockerignore"


def _dockerignore_lines() -> list[str]:
    """讀取根 .dockerignore 的有效規則行（去空行與註解）。"""
    raw = ROOT_DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in raw if line.strip() and not line.strip().startswith("#")]


class TestDispatchInProxyImage:
    """dispatch 源碼必須隨 proxy 單一映像出貨（取代原獨立 Dockerfile 契約）。"""

    def test_no_standalone_dispatch_dockerfile(self):
        """ADR-0082：dispatch 不再有獨立 Dockerfile（防止打包工件復活造成雙拓撲漂移）。"""
        assert not (ROOT / "ToukaDispatch" / "Dockerfile").exists(), (
            "ToukaDispatch/Dockerfile 不應存在——dispatch 已隨 proxy 單一映像出貨（ADR-0082）；"
            "若要恢復獨立容器，請先以新 ADR 取代 ADR-0082"
        )

    def test_proxy_dockerfile_uses_full_copy(self):
        """root-context Dockerfile 必須 COPY . .（禁令 47 物理完整性，涵蓋 dispatch 源碼）。

        接受可選 ``--chown=`` 旗標（ADR-0117：``COPY --chown=appuser:appuser . .``——
        整 repo 複製保證不變，且避免事後 ``chown -R`` 把映像內 venv 重複進第二 layer）。
        """
        assert PROXY_DOCKERFILE.exists(), "ToukaProxy/Dockerfile 必須存在"
        content = PROXY_DOCKERFILE.read_text(encoding="utf-8")
        assert re.search(r"COPY(\s+--chown=\S+)?\s+\.\s+\./?\s*$", content, re.MULTILINE), (
            "ToukaProxy/Dockerfile 必須使用 COPY . .（可帶 --chown；黑名單制，禁令 47）；"
            "選擇性 COPY 會漏掉新增模組（如 dispatch 的 session_manager.py）"
        )

    def test_dockerignore_does_not_exclude_dispatch(self):
        """根 .dockerignore 不得排除 ToukaDispatch——否則 supervisor spawn 即崩潰。"""
        lines = _dockerignore_lines()
        offenders = [
            ln for ln in lines if ln.rstrip("/") in ("ToukaDispatch", "ToukaDispatch/**")
        ]
        assert not offenders, (
            f"根 .dockerignore 排除了 dispatch 源碼：{offenders}——dispatch 隨 proxy "
            "單一映像出貨（ADR-0082），server.py/session_manager.py/config/ 必須進映像"
        )

    def test_dockerignore_does_not_exclude_memory(self):
        """ToukaMemory 同理（整合模式源碼必須進映像，proxy 與 dispatch 都依賴 memory_server）。"""
        lines = _dockerignore_lines()
        offenders = [
            ln for ln in lines if ln.rstrip("/") in ("ToukaMemory", "ToukaMemory/**")
        ]
        assert not offenders, f"根 .dockerignore 不得排除 ToukaMemory：{offenders}"


class TestImageSecretBlacklist:
    """安全黑名單：密鑰／活體設定／聖域資料絕不可烤進公開映像（ADR-0082）。"""

    def test_dockerignore_exists_and_excludes_secrets(self):
        """根 .dockerignore 必須存在且排除 .env、活體 config JSON、data/。"""
        assert ROOT_DOCKERIGNORE.exists(), (
            "根 .dockerignore 必須存在（root build context 的安全黑名單）——"
            "缺檔時 COPY . . 會把 .env 與活體 config 烤進映像"
        )
        lines = _dockerignore_lines()
        for required in (".env", "data/", "ToukaProxy/config/*.json"):
            assert required in lines, f"根 .dockerignore 必須含 `{required}`（密鑰/資料不入映像）"

    def test_dockerignore_excludes_private_internal_docs(self):
        """私人/內部文件不得烤進映像——`.gitignore` 擋 git 但擋不了 `COPY . .`。

        公開發佈體檢（2026-06-12）實測映像 `/app` 內含這些 gitignored 私人檔；
        一旦 push 公開 registry 即任何人可讀。一次性密碼檔一併防禦性排除
        （跑過首裝精靈的開發機上 build 時該檔可能在場）。
        """
        lines = _dockerignore_lines()
        for required in (
            "AGENTS.md",
            "README_HANDOVER.md",
            "SNS.md",
            "溝通協調.md",
            "ToukaProxy/config/INITIAL_ADMIN_PASSWORD.txt",
        ):
            assert required in lines, (
                f"根 .dockerignore 必須排除私人/內部檔 `{required}`（映像即將公開散布）"
            )

    def test_dockerignore_is_not_gitignored(self):
        """根 .dockerignore 必須入版控——被 gitignore 的話 fresh clone build 會把密鑰打包進映像。"""
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        rules = [ln.strip() for ln in gitignore if ln.strip() and not ln.strip().startswith("#")]
        assert ".dockerignore" not in rules, (
            ".gitignore 不得忽略 .dockerignore（ADR-0082：安全黑名單必須隨 repo 散布）"
        )
