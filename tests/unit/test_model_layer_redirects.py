"""禁令 22：每個 httpx／httpx2 client 與 Claude SDK client 都明寫不跟隨轉址（model-p0 §2-8）。

純 AST 掃描（不 import 產品碼）。範圍（3a-U2 起）：整個 ``ToukaProxy/src``＋``scripts``，加上會
真的連外的人工 probe（``tests/smoke/manual``）；原本只掃模型層。ADR-0250 B3 起加上 ToukaDispatch、
ToukaHeartbeat、ToukaMemory（``src``／``scripts``）。

- ``httpx``／``httpx2`` 的 ``AsyncClient``／``Client``，以及 anthropic SDK 的
  ``DefaultAsyncHttpxClient``／``DefaultHttpxClient``（SDK 預設 ``follow_redirects=True``），
  呼叫時必須帶字面量 ``follow_redirects=False``；也不得以類別本身當 ``default_factory``。
- Claude SDK 建構子 ``AsyncAnthropic``／``Anthropic`` 必須帶 ``http_client=``，值是當場建立、
  且符合上一條的 client（變數、``None``、只給 ``**kwargs`` 都不算）。
- 3a-U4 起 SDK 建構子也必須明寫 ``base_url=``（ADR-0247）：SDK 缺值時改連環境變數
  ``ANTHROPIC_BASE_URL``，金鑰一起送過去。值不得是 ``None``，也不得讀環境變數。
- R-3a 起只能更嚴：範圍內每個 SDK 建構點的 ``base_url`` 都直接寫 ``ANTHROPIC_API_BASE_URL``，且這個
  名字只能從 ``src.config``（或 ``.config``）匯入、檔內不得另外綁定；SDK 建構子＝anthropic 匯出、
  名稱以 ``Anthropic``／``AsyncAnthropic`` 開頭的類別（含 Bedrock／Vertex／Foundry 等平台）與別名；
  dotted import、別名指派都追得到；``with_options``／``copy`` 帶 ``base_url``／``http_client``
  時套同樣規則（``copy`` 只限追得到 SDK 的接收者）；任何呼叫、``**{...}`` 或屬性指派的
  ``follow_redirects`` 不是字面量 ``False`` 一律違規；不得事後指派 ``.base_url``（SSOT 常數除外）。
- 3b-U4a 起擴到 openai SDK（ADR-0248 D4）：建構子（``AsyncOpenAI``／``OpenAI``／Azure 版與
  ``AsyncClient``／``Client`` 別名）必須帶 ``api_key=``、``base_url=``、``http_client=``，三個都
  不得是 ``None`` 字面量；``api_key``／``base_url`` 也不得讀環境變數（SDK 缺值時改讀
  ``OPENAI_API_KEY``／``OPENAI_BASE_URL``）。``http_client`` 規則同 Claude，openai 的
  ``DefaultAsyncHttpxClient``／``DefaultHttpxClient`` 也算 client（SDK 預設跟隨轉址）。
- R-3b ① 起只能更嚴：openai 的 ``base_url`` 也不得讀 ``OPENAI_BASE_URL``（會被同名環境變數覆寫的
  Settings 欄位；``api_key`` 照舊可讀 Touka 自己的設定）；追得到 openai client 的 ``with_options``／
  ``copy`` 副本一律禁止（副本建構時 SDK 重讀 ``OPENAI_*`` 環境變數）。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SCAN_ROOTS = (
    "ToukaProxy/src",
    "scripts",
    "tests/smoke/manual",
    # ADR-0250 B3：其他子專案也會連外（Dispatch 回呼、Heartbeat 喚醒）
    "ToukaDispatch",
    "ToukaHeartbeat",
    "ToukaMemory/src",
    "ToukaMemory/scripts",
)
_FILES = sorted(
    p.relative_to(ROOT).as_posix() for r in _SCAN_ROOTS for p in (ROOT / r).rglob("*.py")
)
_HTTP_MODULES = {"httpx", "httpx2"}
_CLIENT_CLASSES = {"AsyncClient", "Client"}
_SDK_MODULE = "anthropic"
_SDK_HTTP_CLASSES = {"DefaultAsyncHttpxClient", "DefaultHttpxClient"}
_SDK_ALIASES = {"AsyncClient", "Client"}  # SDK 對 AsyncAnthropic／Anthropic 的別名
_SDK_CLASS = re.compile(r"(Async)?Anthropic\w*")  # 含 AnthropicBedrock／AsyncAnthropicVertex 等
_SDK_COPIES = {"with_options", "copy"}
_OPENAI_MODULE = "openai"
_OPENAI_CLASS = re.compile(r"(Async)?(Azure)?OpenAI")  # 建構子；OpenAIError 等不算
_OPENAI_ENV_NAMES = {"environ", "getenv"}
_OPENAI_BASE_ENV_NAMES = _OPENAI_ENV_NAMES | {"OPENAI_BASE_URL"}  # 可被環境變數覆寫的 Settings 欄位
_BASE_URL_SSOT = "ANTHROPIC_API_BASE_URL"
_CONFIG_MODULE = ("src", "config")
# 違規訊息（測試逐例比對是哪一條規則）
_R_FOLLOW = "缺少 follow_redirects=False"
_R_HTTP = "SDK 沒注入 no-redirect http_client"
_R_BASE = "SDK 沒明寫固定的 base_url"
_R_FACTORY = "以 client 類別本身當 default_factory"
_R_FOLLOW_ANY = "follow_redirects 不是字面量 False"
_R_COPY_KWARGS = "SDK 副本以 **kwargs 傳參"
_R_BASE_ASSIGN = "事後指派 .base_url"
_R_OPENAI_KEY = "openai SDK 沒明寫 api_key"
_R_OPENAI_BASE = "openai SDK 沒明寫 base_url"
_R_OPENAI_COPY = "openai SDK 副本（with_options／copy 會重讀環境變數）"
# base_url 運算式裡出現這些名字＝從環境變數讀（或讀同名 Settings 欄位），一律不算明寫。
_ENV_NAMES = {"environ", "getenv", "ANTHROPIC_BASE_URL"}
# 已知有 client 建構點的檔案：每一檔都必須掃到，找不到＝掃描失準，不得空過。
_MUST_FIND_CLIENTS = [
    "ToukaProxy/src/handlers/adapters/gemini_adapter.py",
    "ToukaProxy/src/handlers/adapters/openai_adapter.py",
    "ToukaProxy/src/services/models/fetcher.py",
    "ToukaProxy/src/services/models/health.py",
    "ToukaProxy/src/routers/models.py",
    "ToukaProxy/src/tools/builtin/research.py",
    "ToukaProxy/src/tools/grok_imagine_server.py",
    # 模型層以外（3a-U2 擴大範圍後納入）
    "ToukaProxy/src/routers/heartbeat.py",
    "ToukaProxy/src/services/mcp_hub_service.py",
    "ToukaProxy/src/tools/editor.py",
    "scripts/curate_skill_catalog.py",
    # ADR-0250 B3：Proxy 以外的子專案
    "ToukaDispatch/server.py",
    "ToukaHeartbeat/proxy_client.py",
]
_MUST_FIND_SDK = [
    "ToukaProxy/src/main.py",
    "ToukaProxy/src/services/completion.py",
    "scripts/run_tool_eval_episode.py",
    "tests/smoke/manual/live_chat_probe.py",
]
_MUST_FIND_OPENAI = ["ToukaProxy/src/handlers/adapters/responses_adapter.py"]


@dataclass
class _Scan:
    clients: int = 0
    sdks: int = 0
    # base_url= 直接寫 ANTHROPIC_API_BASE_URL 的 SDK 建構點；副本沒帶 base_url（沿用原 client）也算
    sdks_on_ssot_base_url: int = 0
    openai_sdks: int = 0  # openai SDK 建構點（3b-U4a）
    bad: list[str] = field(default_factory=list)


def _follows_no_redirects(call: ast.Call) -> bool:
    return any(
        kw.arg == "follow_redirects"
        and isinstance(kw.value, ast.Constant)
        and kw.value.value is False
        for kw in call.keywords
    )


def _pins_base_url(call: ast.Call) -> bool:
    """SDK 建構子明寫 base_url，且不是 None、不讀環境變數。"""
    given = [kw.value for kw in call.keywords if kw.arg == "base_url"]
    if not given:
        return False
    for sub in ast.walk(given[0]):
        if isinstance(sub, ast.Constant) and (sub.value is None or sub.value in _ENV_NAMES):
            return False
        if isinstance(sub, ast.Attribute) and sub.attr in _ENV_NAMES:
            return False
        if isinstance(sub, ast.Name) and sub.id in _ENV_NAMES:
            return False
    return True


def _explicit(call: ast.Call, name: str) -> bool:
    """openai 建構子明寫了這個參數，值不是 None、不讀環境變數（只給 ``**kwargs`` 不算）。"""
    given = [kw.value for kw in call.keywords if kw.arg == name]
    env = _OPENAI_BASE_ENV_NAMES if name == "base_url" else _OPENAI_ENV_NAMES
    return bool(given) and not any(
        (isinstance(sub, ast.Constant) and sub.value is None)
        or (isinstance(sub, ast.Attribute) and sub.attr in env)
        or (isinstance(sub, ast.Name) and sub.id in env)
        for sub in ast.walk(given[0])
    )


def _is_false(expr: ast.expr) -> bool:
    return isinstance(expr, ast.Constant) and expr.value is False


def _assigned_pairs(node: ast.AST) -> list[tuple[ast.expr, ast.expr | None]]:
    """指派的 (目標, 值)；tuple 對 tuple 逐項拆開，+= 這類的值記 None。"""
    if isinstance(node, ast.AugAssign):
        return [(node.target, None)]
    if isinstance(node, ast.AnnAssign):
        return [(node.target, node.value)] if node.value is not None else []
    if not isinstance(node, ast.Assign):
        return []
    pairs: list[tuple[ast.expr, ast.expr | None]] = []
    for target in node.targets:
        if (isinstance(target, ast.Tuple) and isinstance(node.value, ast.Tuple)
                and len(target.elts) == len(node.value.elts)):
            pairs += list(zip(target.elts, node.value.elts, strict=True))
        else:
            pairs.append((target, node.value))
    return pairs


def _classify(module: str, cls: str) -> str | None:
    root = module.split(".", 1)[0]
    if root in _HTTP_MODULES:
        return "client" if cls in _CLIENT_CLASSES else None
    if root == _OPENAI_MODULE:
        if cls in _SDK_HTTP_CLASSES:
            return "client"
        return "openai" if _OPENAI_CLASS.fullmatch(cls) or cls in _SDK_ALIASES else None
    if root != _SDK_MODULE:
        return None
    if cls in _SDK_HTTP_CLASSES:
        return "client"
    sdk_name = _SDK_CLASS.fullmatch(cls) and not cls.endswith("Error")  # AnthropicError 是例外
    return "sdk" if sdk_name or cls in _SDK_ALIASES else None


def _scan(source: str, label: str) -> _Scan:
    """掃一段原始碼：數 client／SDK 建構點並列出違規。"""
    tree = ast.parse(source)
    watched = _HTTP_MODULES | {_SDK_MODULE, _OPENAI_MODULE}
    modules: dict[str, str] = {}  # 綁定名 → 模組路徑（import a.b 綁 a；import a.b as x 綁 x）
    names: dict[str, tuple[str, str]] = {}  # 綁定名 → (模組, 名稱)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".", 1)[0]
                if root in watched:
                    modules[a.asname or root] = a.name if a.asname else root
        elif (isinstance(node, ast.ImportFrom) and node.module and node.level == 0
              and node.module.split(".", 1)[0] in watched):
            names |= {a.asname or a.name: (node.module, a.name) for a in node.names}

    def resolve(expr: ast.expr) -> tuple[str, str] | None:
        parts: list[str] = []
        while isinstance(expr, ast.Attribute):
            parts.insert(0, expr.attr)
            expr = expr.value
        if not isinstance(expr, ast.Name):
            return None
        if not parts:
            return names.get(expr.id)
        if expr.id in modules:
            base = modules[expr.id]
        elif expr.id in names:
            base = ".".join(names[expr.id])
        else:
            return None
        return ".".join([base, *parts[:-1]]), parts[-1]

    def kind(expr: ast.expr) -> str | None:
        """'client'（httpx 類 client）、'sdk'（Claude SDK 建構子）或 None。"""
        found = resolve(expr)
        return _classify(*found) if found else None

    # 別名指派（k = anthropic.AsyncAnthropic；含型別標註與 tuple 指派）：追到不動點
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            for target, value in _assigned_pairs(node):
                if (isinstance(target, ast.Name) and target.id not in names
                        and value is not None and kind(value)):
                    names[target.id] = resolve(value)  # type: ignore[assignment]
                    changed = True

    def is_copy(call: ast.expr) -> bool:
        return (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and call.func.attr in _SDK_COPIES)

    # 追得到 SDK 的物件（c = AsyncAnthropic(...)、d = c.with_options(...)）：copy 規則只套用在它們；
    # openai 的物件另記，副本一律禁止（R-3b ①）
    objects: dict[str, set[str]] = {"sdk": set(), "openai": set()}

    def is_object(expr: ast.expr, family: str) -> bool:
        if isinstance(expr, ast.Call):
            return kind(expr.func) == family or (is_copy(expr)
                                                 and is_object(expr.func.value, family))
        return ast.unparse(expr) in objects[family]

    def is_sdk_object(expr: ast.expr) -> bool:
        return is_object(expr, "sdk")

    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            for target, value in _assigned_pairs(node):
                key = ast.unparse(target)
                for family, known in objects.items():
                    if (isinstance(target, ast.Name | ast.Attribute) and key not in known
                            and isinstance(value, ast.Call) and is_object(value, family)):
                        known.add(key)
                        changed = True

    def fresh_no_redirect_client(call: ast.Call) -> bool:
        given = [kw.value for kw in call.keywords if kw.arg == "http_client"]
        return bool(given and isinstance(given[0], ast.Call) and kind(given[0].func) == "client")

    def on_ssot(call: ast.Call) -> bool:
        return any(kw.arg == "base_url" and isinstance(kw.value, ast.Name)
                   and kw.value.id == _BASE_URL_SSOT for kw in call.keywords)

    scan = _Scan()

    def bad(node: ast.AST, rule: str) -> None:
        scan.bad.append(f"{label}:{node.lineno} {rule}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and kind(node.func) == "client":
            scan.clients += 1
            if not _follows_no_redirects(node):
                bad(node, _R_FOLLOW)
        elif isinstance(node, ast.Call) and kind(node.func) == "sdk":
            scan.sdks += 1
            if not fresh_no_redirect_client(node):
                bad(node, _R_HTTP)
            if not _pins_base_url(node):
                bad(node, _R_BASE)
            scan.sdks_on_ssot_base_url += on_ssot(node)
        elif isinstance(node, ast.Call) and kind(node.func) == "openai":
            scan.openai_sdks += 1
            if not fresh_no_redirect_client(node):
                bad(node, _R_HTTP)
            for arg, rule in (("api_key", _R_OPENAI_KEY), ("base_url", _R_OPENAI_BASE)):
                if not _explicit(node, arg):
                    bad(node, rule)
        elif is_copy(node) and is_object(node.func.value, "openai"):  # type: ignore[attr-defined]
            bad(node, _R_OPENAI_COPY)
        elif is_copy(node) and (node.func.attr == "with_options"  # type: ignore[attr-defined]
                                or is_sdk_object(node.func.value)):  # type: ignore[attr-defined]
            given = {kw.arg for kw in node.keywords}
            if None in given:
                bad(node, _R_COPY_KWARGS)
            if given & {"base_url", "http_client"}:
                scan.sdks += 1
                if "http_client" in given and not fresh_no_redirect_client(node):
                    bad(node, _R_HTTP)
                if "base_url" in given and not _pins_base_url(node):
                    bad(node, _R_BASE)
                scan.sdks_on_ssot_base_url += "base_url" not in given or on_ssot(node)
        elif isinstance(node, ast.keyword) and node.arg == "default_factory" and kind(node.value):
            if kind(node.value) == "sdk":
                scan.sdks += 1
            elif kind(node.value) == "openai":
                scan.openai_sdks += 1
            else:
                scan.clients += 1
            bad(node.value, _R_FACTORY)
        if isinstance(node, ast.Call) and kind(node.func) != "client":
            for kw in node.keywords:
                if kw.arg == "follow_redirects" and not _is_false(kw.value):
                    bad(node, _R_FOLLOW_ANY)
                if kw.arg is None and isinstance(kw.value, ast.Dict):  # **{'follow_redirects': ...}
                    for key, value in zip(kw.value.keys, kw.value.values, strict=True):
                        if (isinstance(key, ast.Constant) and key.value == "follow_redirects"
                                and not _is_false(value)):
                            bad(node, _R_FOLLOW_ANY)
        for target, value in _assigned_pairs(node):
            if not isinstance(target, ast.Attribute):
                continue
            if target.attr == "follow_redirects" and (value is None or not _is_false(value)):
                bad(node, _R_FOLLOW_ANY)
            pinned = isinstance(value, ast.Name) and value.id == _BASE_URL_SSOT
            if target.attr == "base_url" and not pinned:  # SDK 1.11 與 httpx 都有 setter
                bad(node, _R_BASE_ASSIGN)
    return scan


def _ssot_offenders(rel: str, source: str) -> list[str]:
    """全範圍 SSOT 比對：SDK 建構點都直接寫常數；檔內有 base_url 時名字必須來自 src.config。"""
    scan = _scan(source, rel)
    if not scan.sdks:
        return []
    offenders = []
    if scan.sdks_on_ssot_base_url != scan.sdks:
        offenders.append(f"{rel}: {scan.sdks_on_ssot_base_url}/{scan.sdks}")
    tree = ast.parse(source)
    if any(isinstance(node, ast.keyword) and node.arg == "base_url" for node in ast.walk(tree)):
        offenders += _ssot_name_problems(source, rel)  # 只帶 http_client 的副本不必匯入常數
    return offenders


def _module_parts(rel: str) -> list[str]:
    """repo 相對路徑 → 匯入時的模組路徑（ToukaProxy 底下以 src 為頂層）。"""
    parts = rel.removeprefix("ToukaProxy/").removesuffix(".py").split("/")
    return parts[:-1] if parts[-1] == "__init__" else parts


def _ssot_name_problems(source: str, rel: str) -> list[str]:
    """ANTHROPIC_API_BASE_URL 必須從 src.config 匯入，檔內不得另外綁定（指派、def、參數、別名）。"""
    tree = ast.parse(source)
    package = _module_parts(rel)
    if not rel.endswith("__init__.py"):
        package = package[:-1]
    imported, problems = 0, []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if (alias.asname or alias.name) != _BASE_URL_SSOT:
                    continue
                keep = len(package) - (node.level - 1) if node.level else 0
                base = package[:keep] if keep >= 0 else ["<invalid>"]
                origin = (*base, *(node.module or "").split("."))
                if origin == _CONFIG_MODULE and alias.name == _BASE_URL_SSOT:
                    imported += 1
                else:
                    problems.append(f"{rel}:{node.lineno} 從 {'.'.join(origin)} 匯入")
        elif isinstance(node, ast.Import):
            problems += [f"{rel}:{node.lineno} import 綁定" for a in node.names
                         if a.asname == _BASE_URL_SSOT]
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
            problems += [f"{rel}:{node.lineno} 檔內綁定"] if node.id == _BASE_URL_SSOT else []
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            problems += [f"{rel}:{node.lineno} 檔內定義"] if node.name == _BASE_URL_SSOT else []
        elif isinstance(node, ast.arg) and node.arg == _BASE_URL_SSOT:
            problems.append(f"{rel}:{node.lineno} 參數綁定")
        elif isinstance(node, ast.Global | ast.Nonlocal) and _BASE_URL_SSOT in node.names:
            problems.append(f"{rel}:{node.lineno} global 綁定")
    if imported != 1:
        problems.append(f"{rel}: 從 src.config 匯入 {imported} 次（須恰好 1 次）")
    return problems


@cache
def _scan_file(rel: str) -> _Scan:
    return _scan((ROOT / rel).read_text(encoding="utf-8"), rel)


def test_no_client_in_scope_follows_redirects():
    bad = [item for rel in _FILES for item in _scan_file(rel).bad]
    assert not bad, "禁令 22：" + "；".join(bad)


def test_scope_covers_whole_proxy_and_scripts():
    """範圍不得縮回模型層：每個根目錄都要在，總數與建構點數有下限。"""
    roots = {rel.split("/", 1)[0] for rel in _FILES}
    assert roots == {
        "ToukaProxy", "scripts", "tests", "ToukaDispatch", "ToukaHeartbeat", "ToukaMemory"
    }
    assert sum(rel.startswith("ToukaProxy/src/") for rel in _FILES) >= 300
    assert sum(rel.startswith("scripts/") for rel in _FILES) >= 40
    assert sum(rel.startswith("ToukaDispatch/") for rel in _FILES) >= 3
    assert sum(rel.startswith("ToukaHeartbeat/") for rel in _FILES) >= 10
    assert sum(rel.startswith("ToukaMemory/src/") for rel in _FILES) >= 25
    assert sum(rel.startswith("ToukaMemory/scripts/") for rel in _FILES) >= 1
    # ADR-0250：Proxy 排程引擎（1 個 client）已刪，Dispatch 與 Heartbeat 各加 1 個
    assert sum(_scan_file(rel).clients for rel in _FILES) >= 35
    assert sum(_scan_file(rel).sdks for rel in _FILES) >= 4
    assert sum(_scan_file(rel).openai_sdks for rel in _FILES) >= 1


@pytest.mark.parametrize("rel", _MUST_FIND_CLIENTS)
def test_known_client_files_are_not_vacuous(rel: str):
    assert rel in _FILES and _scan_file(rel).clients >= 1, f"{rel}: 找不到任何 httpx client 建構點"


@pytest.mark.parametrize("rel", _MUST_FIND_OPENAI)
def test_known_openai_construction_points_are_not_vacuous(rel: str):
    assert rel in _FILES and _scan_file(rel).openai_sdks >= 1, f"{rel}: 找不到 openai SDK 建構點"


@pytest.mark.parametrize("rel", _MUST_FIND_SDK)
def test_known_sdk_construction_points_are_not_vacuous(rel: str):
    assert rel in _FILES and _scan_file(rel).sdks >= 1, f"{rel}: 找不到 Claude SDK 建構點"


@pytest.mark.parametrize("rel", _MUST_FIND_SDK)
def test_known_sdk_construction_points_use_config_base_url(rel: str):
    """3a-U4：已知建構點的 base_url 一律直接寫 config 的 ANTHROPIC_API_BASE_URL（SSOT）。"""
    scan = _scan_file(rel)
    assert scan.sdks >= 1 and scan.sdks_on_ssot_base_url == scan.sdks, rel
    assert _ssot_name_problems((ROOT / rel).read_text(encoding="utf-8"), rel) == []


def test_every_sdk_construction_point_in_scope_uses_the_config_constant():
    """R-3a：整個範圍（不只已知 4 檔）的 SDK 建構點都直接寫這個常數，名字也只來自 src.config。"""
    offenders = [item for rel in _FILES if _scan_file(rel).sdks
                 for item in _ssot_offenders(rel, (ROOT / rel).read_text(encoding="utf-8"))]
    assert offenders == []


def test_config_base_url_constant_is_a_plain_string_literal():
    """R-3a：常數本身是字串字面量，不是 Settings 欄位、不讀環境變數（只綁定一次）。"""
    tree = ast.parse((ROOT / "ToukaProxy/src/config.py").read_text(encoding="utf-8"))
    stores = [node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
              and isinstance(node.ctx, ast.Store)
              and node.id in {_BASE_URL_SSOT, "ANTHROPIC_BASE_URL"}]
    values = [node.value for node in tree.body if isinstance(node, ast.Assign)
              and [t.id for t in node.targets if isinstance(t, ast.Name)] == [_BASE_URL_SSOT]]
    assert stores == [_BASE_URL_SSOT]
    assert len(values) == 1 and isinstance(values[0], ast.Constant)
    assert values[0].value == "https://api.anthropic.com"


def _rules(scan: _Scan) -> set[str]:
    return {item.split(" ", 1)[1] for item in scan.bad}


@pytest.mark.parametrize(("source", "rules"), [
    ("import httpx\nhttpx.AsyncClient(follow_redirects=False)", set()),
    ("import httpx\nhttpx.AsyncClient(timeout=9)", {_R_FOLLOW}),
    ("import httpx\nhttpx.Client(follow_redirects=True)", {_R_FOLLOW}),
    ("import httpx as hx\nhx.Client(**opts)", {_R_FOLLOW}),
    ("from httpx2 import AsyncClient as C\nC()", {_R_FOLLOW}),
    ("import httpx\nfield(default_factory=httpx.AsyncClient)", {_R_FACTORY}),
    ("import httpx\nfield(default_factory=lambda: httpx.Client(follow_redirects=False))", set()),
    ("from anthropic import DefaultAsyncHttpxClient\nDefaultAsyncHttpxClient()", {_R_FOLLOW}),
    ("import anthropic\nanthropic.DefaultHttpxClient(follow_redirects=False)", set()),
    # R-3a：單次請求、事後改屬性、非字面量的 follow_redirects；dotted import 與別名指派
    ("import httpx\nc = httpx.AsyncClient(follow_redirects=False)\n"
     "c.get(url, follow_redirects=True)", {_R_FOLLOW_ANY}),
    ("import httpx\nc = httpx.Client(follow_redirects=False)\nc.follow_redirects = True",
     {_R_FOLLOW_ANY}),
    ("import httpx\nhttpx.Client(follow_redirects=False)\nhttpx.get(url, follow_redirects=flag)",
     {_R_FOLLOW_ANY}),
    ("import httpx._client\nhttpx._client.AsyncClient()", {_R_FOLLOW}),
    ("import httpx\nK = httpx.AsyncClient\nK()", {_R_FOLLOW}),
    # 3b-U4a：openai SDK 的 http client 也算（SDK 預設跟隨轉址）
    ("from openai import DefaultAsyncHttpxClient\nDefaultAsyncHttpxClient()", {_R_FOLLOW}),
    ("import openai\nopenai.DefaultHttpxClient(follow_redirects=False)", set()),
])
def test_scanner_detects_violations(source: str, rules: set[str]):
    scan = _scan(source, "snippet")
    assert (scan.clients, _rules(scan)) == (1, rules)


_SDK_IMPORTS = "from anthropic import AsyncAnthropic, Anthropic, DefaultAsyncHttpxClient\n"
_SAFE = "http_client=DefaultAsyncHttpxClient(follow_redirects=False)"
_SSOT = "base_url=ANTHROPIC_API_BASE_URL"
_BOTH = {_R_HTTP, _R_BASE}


@pytest.mark.parametrize(("source", "rules"), [
    (_SDK_IMPORTS + "AsyncAnthropic(api_key='fake-key')", _BOTH),
    (_SDK_IMPORTS + f"AsyncAnthropic({_SAFE}, {_SSOT})", set()),
    (_SDK_IMPORTS + f"AsyncAnthropic({_SAFE}, base_url='https://api.anthropic.com')", set()),
    # 3a-U4：http_client 沒問題，但缺 base_url、None、讀環境變數（含同名 Settings 欄位）都算違規
    (_SDK_IMPORTS + f"AsyncAnthropic({_SAFE})", {_R_BASE}),
    (_SDK_IMPORTS + f"AsyncAnthropic({_SAFE}, base_url=None)", {_R_BASE}),
    (_SDK_IMPORTS + f"AsyncAnthropic({_SAFE}, base_url=url or None)", {_R_BASE}),
    (_SDK_IMPORTS + f"AsyncAnthropic({_SAFE}, base_url=os.environ.get('ANTHROPIC_BASE_URL'))",
     {_R_BASE}),
    (_SDK_IMPORTS + f"AsyncAnthropic({_SAFE}, base_url=os.getenv('X', 'https://a.test'))",
     {_R_BASE}),
    (_SDK_IMPORTS + f"Anthropic({_SAFE}, base_url=environ['TOUKA_URL'])", {_R_BASE}),
    (_SDK_IMPORTS + f"AsyncAnthropic({_SAFE}, base_url=settings.ANTHROPIC_BASE_URL)", {_R_BASE}),
    (_SDK_IMPORTS + f"AsyncAnthropic({_SAFE}, base_url=ANTHROPIC_BASE_URL)", {_R_BASE}),
    (_SDK_IMPORTS + f"AsyncAnthropic({_SAFE}, **{{'base_url': None}})", {_R_BASE}),
    # R-3a：base_url 合格時，http_client 規則仍要單獨抓到（不靠 base_url 規則陪著紅）
    (_SDK_IMPORTS + f"AsyncAnthropic(http_client=DefaultAsyncHttpxClient(), {_SSOT})",
     {_R_FOLLOW}),
    (_SDK_IMPORTS + "Anthropic(http_client=DefaultAsyncHttpxClient(follow_redirects=True), "
     f"{_SSOT})", {_R_FOLLOW}),
    (_SDK_IMPORTS + f"AsyncAnthropic(http_client=shared_client, {_SSOT})", {_R_HTTP}),
    (_SDK_IMPORTS + f"AsyncAnthropic(http_client=None, {_SSOT})", {_R_HTTP}),
    (_SDK_IMPORTS + f"AsyncAnthropic(api_key='fake-key', {_SSOT})", {_R_HTTP}),
    (_SDK_IMPORTS + "Anthropic(**opts)", _BOTH),
    (_SDK_IMPORTS + "field(default_factory=AsyncAnthropic)", {_R_FACTORY}),
    ("import anthropic as a, httpx2\n"
     "a.Anthropic(http_client=httpx2.Client(follow_redirects=False), base_url=URL)", set()),
    ("from anthropic import AsyncAnthropic as A\nA(api_key='fake-key')", _BOTH),
    ("import anthropic\nanthropic.AsyncClient(api_key='fake-key')", _BOTH),  # SDK 別名
    # R-3a：平台 client（名稱符合 (Async)?Anthropic\w*）、dotted import、別名指派
    ("from anthropic import AnthropicFoundry\nAnthropicFoundry(api_key='fake-key')", _BOTH),
    ("from anthropic.lib.bedrock import AsyncAnthropicBedrock\nAsyncAnthropicBedrock()", _BOTH),
    ("import anthropic\nanthropic.AnthropicVertex(region='us-east5')", _BOTH),
    ("import anthropic\nanthropic.lib.google_cloud.AnthropicGoogleCloud()", _BOTH),
    ("from anthropic import AnthropicAWS, DefaultHttpxClient\nAnthropicAWS("
     f"http_client=DefaultHttpxClient(follow_redirects=False), {_SSOT})", set()),
    ("import anthropic.types\nanthropic.AsyncAnthropic()", _BOTH),
    ("import anthropic.lib.bedrock as br\nbr.AnthropicBedrock()", _BOTH),
    ("from anthropic._client import AsyncAnthropic\nAsyncAnthropic()", _BOTH),
    ("import anthropic\nk = anthropic.AsyncAnthropic\nk(api_key='fake-key')", _BOTH),
    ("from anthropic import AsyncAnthropic\nk = AsyncAnthropic\nj = k\nj()", _BOTH),
])
def test_scanner_detects_sdk_violations(source: str, rules: set[str]):
    scan = _scan(source, "snippet")
    assert (scan.sdks, _rules(scan)) == (1, rules)


_OAI = "import os\nfrom openai import AsyncOpenAI, OpenAI, DefaultAsyncHttpxClient\n"
_OAI_HTTP = "http_client=DefaultAsyncHttpxClient(follow_redirects=False)"
_OAI_ALL = {_R_HTTP, _R_OPENAI_KEY, _R_OPENAI_BASE}


@pytest.mark.parametrize(("source", "rules"), [
    (_OAI + f"AsyncOpenAI(api_key=key, base_url=url, {_OAI_HTTP})", set()),
    (_OAI + "AsyncOpenAI()", _OAI_ALL),
    (_OAI + "OpenAI(**opts)", _OAI_ALL),
    # api_key：缺、None、`x or None`、讀環境變數
    (_OAI + f"AsyncOpenAI(base_url=url, {_OAI_HTTP})", {_R_OPENAI_KEY}),
    (_OAI + f"AsyncOpenAI(api_key=None, base_url=url, {_OAI_HTTP})", {_R_OPENAI_KEY}),
    (_OAI + f"AsyncOpenAI(api_key=key or None, base_url=url, {_OAI_HTTP})", {_R_OPENAI_KEY}),
    (_OAI + f"AsyncOpenAI(api_key=os.environ['OPENAI_API_KEY'], base_url=url, {_OAI_HTTP})",
     {_R_OPENAI_KEY}),
    # base_url：缺、None、讀環境變數
    (_OAI + f"AsyncOpenAI(api_key=key, {_OAI_HTTP})", {_R_OPENAI_BASE}),
    (_OAI + f"AsyncOpenAI(api_key=key, base_url=None, {_OAI_HTTP})", {_R_OPENAI_BASE}),
    (_OAI + f"AsyncOpenAI(api_key=key, base_url=os.getenv('OPENAI_BASE_URL'), {_OAI_HTTP})",
     {_R_OPENAI_BASE}),
    (_OAI + f"AsyncOpenAI(api_key=key, base_url=OPENAI_BASE_URL, {_OAI_HTTP})", {_R_OPENAI_BASE}),
    (_OAI + f"AsyncOpenAI(api_key=key, base_url=settings.OPENAI_BASE_URL, {_OAI_HTTP})",
     {_R_OPENAI_BASE}),
    (_OAI + f"AsyncOpenAI(api_key=settings.OPENAI_API_KEY, base_url=url, {_OAI_HTTP})", set()),
    # 副本：追得到 openai client 的 with_options／copy 一律禁止
    (_OAI + f"c = AsyncOpenAI(api_key=key, base_url=url, {_OAI_HTTP})\nc.with_options(timeout=5)",
     {_R_OPENAI_COPY}),
    (_OAI + f"self.c = AsyncOpenAI(api_key=key, base_url=url, {_OAI_HTTP})\n"
     "self.c.copy(default_headers={})", {_R_OPENAI_COPY}),
    (_OAI + f"AsyncOpenAI(api_key=key, base_url=url, {_OAI_HTTP}).with_options(max_retries=0)",
     {_R_OPENAI_COPY}),
    # http_client：缺、None、變數、會跟隨轉址的 client
    (_OAI + "AsyncOpenAI(api_key=key, base_url=url)", {_R_HTTP}),
    (_OAI + "AsyncOpenAI(api_key=key, base_url=url, http_client=None)", {_R_HTTP}),
    (_OAI + "AsyncOpenAI(api_key=key, base_url=url, http_client=shared)", {_R_HTTP}),
    (_OAI + "AsyncOpenAI(api_key=key, base_url=url, http_client=DefaultAsyncHttpxClient())",
     {_R_FOLLOW}),
    # 別名、Azure、dotted import、別名指派
    ("import openai\nopenai.AsyncClient(api_key=key)", {_R_HTTP, _R_OPENAI_BASE}),
    ("import openai.lib.azure\nopenai.lib.azure.AsyncAzureOpenAI()", _OAI_ALL),
    ("from openai import AsyncOpenAI\nk = AsyncOpenAI\nk()", _OAI_ALL),
    ("import httpx2, openai\nopenai.AzureOpenAI(api_key=key, base_url=url, "
     "http_client=httpx2.Client(follow_redirects=False))", set()),
])
def test_scanner_detects_openai_violations(source: str, rules: set[str]):
    scan = _scan(source, "snippet")
    assert (scan.openai_sdks, scan.sdks, _rules(scan)) == (1, 0, rules)


@pytest.mark.parametrize("source", [
    "import openai\nopenai.Timeout(5)",
    "import openai\nraise openai.OpenAIError('x')",
    "import anthropic\nanthropic.Timeout(5)",  # SDK 其他類別不算建構點
    "import anthropic\nraise anthropic.AnthropicError('x')",
    "client.with_options(api_key='fake-key', max_retries=0)",  # 沒碰 base_url／http_client
])
def test_scanner_ignores_non_construction_calls(source: str):
    scan = _scan(source, "snippet")
    assert (scan.clients, scan.sdks, scan.openai_sdks, scan.bad) == (0, 0, 0, [])


@pytest.mark.parametrize(("source", "sdks", "rules"), [
    ("client.with_options(base_url=os.environ['ANTHROPIC_BASE_URL'])", 1, {_R_BASE}),
    ("client.with_options(http_client=shared_client)", 1, {_R_HTTP}),
    (_SDK_IMPORTS + f"c = AsyncAnthropic({_SAFE}, {_SSOT})\n"
     "c.copy(http_client=None, base_url=None)", 2, _BOTH),
    (_SDK_IMPORTS + f"self.c = AsyncAnthropic({_SAFE}, {_SSOT})\nd = self.c.with_options()\n"
     "d.copy(**opts)", 1, {_R_COPY_KWARGS}),
    ("client.with_options(**opts)", 0, {_R_COPY_KWARGS}),
    (_SDK_IMPORTS + f"client.with_options({_SAFE}, {_SSOT})", 1, set()),
    # 誤報 b：追不到 SDK 的物件的 copy 不算 SDK 副本（with_options 仍一律檢查；家族區分留給 3b）
    ("settings.copy(**updates)", 0, set()),
    ("model.copy(base_url=os.environ['X'], http_client=None)", 0, set()),
])
def test_scanner_applies_sdk_rules_to_copies(source: str, sdks: int, rules: set[str]):
    scan = _scan(source, "snippet")
    assert (scan.sdks, _rules(scan)) == (sdks, rules)


@pytest.mark.parametrize(("call", "on_ssot"), [
    (f"AsyncAnthropic({_SAFE}, base_url=ANTHROPIC_API_BASE_URL)", 1),
    (f"AsyncAnthropic({_SAFE}, base_url=OTHER_BASE_URL)", 0),
    (f"AsyncAnthropic({_SAFE}, base_url='https://api.anthropic.com')", 0),
    (f"client.with_options({_SAFE})", 1),  # 副本沿用原 client 的 base_url
    ("client.with_options(base_url='https://api.anthropic.com')", 0),
])
def test_scanner_counts_only_the_config_base_url(call: str, on_ssot: int):
    scan = _scan(_SDK_IMPORTS + call, "snippet")
    assert (scan.sdks, scan.sdks_on_ssot_base_url, scan.bad) == (1, on_ssot, [])


_IMPORT_SSOT = "from src.config import ANTHROPIC_API_BASE_URL\n"


@pytest.mark.parametrize(("source", "rules"), [
    ("c.base_url = os.environ['ANTHROPIC_BASE_URL']", {_R_BASE_ASSIGN}),
    ("c.base_url += '/evil'", {_R_BASE_ASSIGN}),
    ("a, c.base_url = 1, url", {_R_BASE_ASSIGN}),
    ("c.base_url = ANTHROPIC_API_BASE_URL", set()),
    ("c.get(url, **{'follow_redirects': True})", {_R_FOLLOW_ANY}),
    ("c.get(url, **{'follow_redirects': False})", set()),
    ("c.follow_redirects += 1", {_R_FOLLOW_ANY}),
    ("import anthropic\nA: type = anthropic.AsyncAnthropic\nA()", _BOTH),
    ("import anthropic, httpx\nA, H = anthropic.AsyncAnthropic, httpx.AsyncClient\nH()\nA()",
     {_R_FOLLOW, _R_HTTP, _R_BASE}),
])
def test_scanner_catches_other_bypasses(source: str, rules: set[str]):
    assert _rules(_scan(source, "snippet")) == rules


@pytest.mark.parametrize(("source", "offends"), [
    (_IMPORT_SSOT + _SDK_IMPORTS + f"AsyncAnthropic({_SAFE}, base_url=self.x)\n", True),
    (_IMPORT_SSOT + _SDK_IMPORTS + f"AsyncAnthropic({_SAFE}, {_SSOT})\n", False),
    (_SDK_IMPORTS + f"AsyncAnthropic({_SAFE}, {_SSOT})\n", True),  # 常數不是從 config 匯入
    (_SDK_IMPORTS + f"client.with_options({_SAFE})\n", False),  # 誤報 a：沒 base_url 不必匯入
])
def test_whole_scope_ssot_check_on_a_written_file(tmp_path, source: str, offends: bool):
    """全範圍比對本身的反例：寫成檔案再走同一個函式（不只單句掃描）。"""
    path = tmp_path / "ToukaProxy" / "src" / "services" / "probe.py"
    path.parent.mkdir(parents=True)
    path.write_text(source, encoding="utf-8")
    rel = path.relative_to(tmp_path).as_posix()
    assert bool(_ssot_offenders(rel, path.read_text(encoding="utf-8"))) is offends


@pytest.mark.parametrize(("source", "rel", "ok"), [
    (_IMPORT_SSOT, "ToukaProxy/src/services/x.py", True),
    ("from .config import ANTHROPIC_API_BASE_URL\n", "ToukaProxy/src/main.py", True),
    ("from ..config import ANTHROPIC_API_BASE_URL\n", "ToukaProxy/src/services/x.py", True),
    (_IMPORT_SSOT, "scripts/run_x.py", True),
    ("from .config import ANTHROPIC_API_BASE_URL\n", "ToukaProxy/src/routers/x.py", False),
    ("from .config import ANTHROPIC_API_BASE_URL\n", "scripts/run_x.py", False),
    ("from src.settings import ANTHROPIC_API_BASE_URL\n", "ToukaProxy/src/services/x.py", False),
    ("from src.config import OTHER as ANTHROPIC_API_BASE_URL\n", "scripts/run_x.py", False),
    ("import os\nANTHROPIC_API_BASE_URL = os.environ['ANTHROPIC_BASE_URL']\n",
     "ToukaProxy/src/services/x.py", False),
    (_IMPORT_SSOT + "ANTHROPIC_API_BASE_URL = os.environ.get('X') or ANTHROPIC_API_BASE_URL\n",
     "ToukaProxy/src/services/x.py", False),
    (_IMPORT_SSOT + "def f(ANTHROPIC_API_BASE_URL=None):\n    return 1\n", "scripts/run_x.py",
     False),
    (_IMPORT_SSOT + _IMPORT_SSOT, "scripts/run_x.py", False),
])
def test_ssot_name_must_come_from_config(source: str, rel: str, ok: bool):
    assert (_ssot_name_problems(source, rel) == []) is ok
