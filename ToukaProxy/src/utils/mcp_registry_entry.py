"""官方 MCP Registry 條目 → hub entry 純映射（SSOT）。

修 F3（ADR-0187）：舊社群來源 Smithery 的安裝命令 ``npx -y @smithery/cli run <name>``
在現行 CLI v4 已無 ``run`` 命令（2026-07-25 `--help` 實證）＝整排貨架失效；社群來源
改接官方 MCP Registry（registry.modelcontextprotocol.io/v0）。本模組是「registry 條目
怎麼變成可安裝 hub entry」的唯一事實來源：

- 只收**最新版且 active**（``_meta["io.modelcontextprotocol.registry/official"]``）。
- 只收 **stdio** transport 的 npm/pypi 套件：npm → ``npx -y <identifier>@<version>``、
  pypi → ``uvx <identifier>==<version>``——與官方條目（``mcp_official_map``）同一套
  執行面，零第三方 launcher 依賴；套件名位**一律釘版**（ADR-0191，registry 條目自帶
  ``packages[].version``；缺版本者不上架）。
- ``environmentVariables`` 的 ``isRequired`` → ``env_requirements``（安裝精靈據此逐項
  提示＝修掉舊來源硬編 ``[]`` 的盲裝）。
- ``packageArguments`` 僅取有具體 ``value`` 者；必填但無值＝無法無人值守安裝 →
  誠實回 ``None``（remote-only／oci／非 stdio 等不可啟動者亦同）。

設計：純函式、零 IO/時間/環境依賴（體例比照 ``mcp_category``／``mcp_official_map``）。
"""
from __future__ import annotations

import re

from src.utils.mcp_category import classify_mcp_category
from src.utils.mcp_remote import validate_remote_url

_OFFICIAL_META_KEY = "io.modelcontextprotocol.registry/official"
# registryType → 執行 runtime（僅此兩種；oci/mcpb 等不落地）。
_SUPPORTED_REGISTRY_RUNTIMES: dict[str, str] = {"npm": "npx", "pypi": "uvx"}
# 只收 streamable HTTP；SSE 已被三家桌面版標為 deprecated（ADR-0192）。
_SUPPORTED_REMOTE_TYPE = "streamable-http"
_NON_ENV_CHARS_RE = re.compile(r"[^A-Z0-9]+")


def _header_env_name(header_name: str) -> str:
    """header 名 → env 變數名（``Authorization`` → ``MCP_AUTHORIZATION``）。

    憑證實值存 ``env``、header 只留 ``${VAR}`` 模板，故需要一個穩定的衍生規則。
    """
    return "MCP_" + _NON_ENV_CHARS_RE.sub("_", header_name.upper()).strip("_")


def _pick_stdio_package(packages: list) -> dict | None:
    """取第一個「stdio ＋ npm/pypi ＋ 有 identifier ＋ 有 version」的套件；無 → None。

    ``version`` 為釘版政策（ADR-0191）的必要條件——缺版本即無從釘起，寧可不上架也
    不放浮動條目進貨架。
    """
    for pkg in packages:
        if not isinstance(pkg, dict):
            continue
        if (pkg.get("transport") or {}).get("type") != "stdio":
            continue
        if not pkg.get("version"):
            continue
        if pkg.get("registryType") in _SUPPORTED_REGISTRY_RUNTIMES and pkg.get("identifier"):
            return pkg
    return None


def _pinned_identifier(command: str, identifier: str, version: str) -> str:
    """套件名 + 釘版標記：npx 走 ``pkg@x.y.z``、uvx 走 PEP 440 的 ``pkg==x.y.z``。"""
    return f"{identifier}@{version}" if command == "npx" else f"{identifier}=={version}"


def _build_args(command: str, identifier: str, package: dict) -> list[str] | None:
    """組啟動引數；必填 packageArgument 無具體值＝無法無人值守安裝 → None。

    套件名位一律帶釘版（ADR-0191）——registry 條目自帶 ``packages[].version``，
    映射時即釘死該版本，安裝端不再抓「未來某個尚未存在的最新版」。
    """
    pinned = _pinned_identifier(command, identifier, package["version"])
    args = ["-y", pinned] if command == "npx" else [pinned]
    for arg in package.get("packageArguments") or []:
        if not isinstance(arg, dict):
            continue
        value = arg.get("value")
        if value:
            if arg.get("type") == "named" and arg.get("name"):
                args.extend([arg["name"], str(value)])
            else:
                args.append(str(value))
        elif arg.get("isRequired"):
            return None
    return args


def registry_item_to_hub_entry(item: dict) -> dict | None:
    """官方 Registry 單一條目 → hub entry；不可安裝者誠實回 ``None``（純函式）。"""
    server = item.get("server") or {}
    name = server.get("name")
    if not name:
        return None
    meta = (item.get("_meta") or {}).get(_OFFICIAL_META_KEY) or {}
    if not meta.get("isLatest") or meta.get("status") != "active":
        return None
    display = server.get("title") or name.rsplit("/", 1)[-1]
    description = server.get("description", "")

    # local-first：同時具備本地套件與遠端端點時取本地（子進程可控、無外呼依賴）。
    package = _pick_stdio_package(server.get("packages") or [])
    if package:
        transport_fields = _stdio_fields(package)
    else:
        transport_fields = _remote_fields(server.get("remotes") or [])
        if transport_fields:
            # 認證說明併入 description：安裝表單只顯示變數名，使用者無從得知
            # 「Authorization 要填 `Bearer xxx`」這類格式要求（零 schema 成本的揭露）。
            description = _append_auth_hint(description, transport_fields.pop("_hint"))
    if not transport_fields:
        return None

    identifier = package.get("identifier", "") if package else ""
    entry: dict = {
        "id": f"registry-{name}",
        "name": display,
        "category": classify_mcp_category(display, description, identifier),
        "description": description,
        "source": "registry",
        "is_official": False,
        "popularity": 0,
        "icon": "Box",
        **transport_fields,
    }
    if package and package["registryType"] == "npm":
        # 供 /mcp/hub 端點抓 npm 真實下載量作排序（誠實數據，非 useCount 假冒）。
        entry["npm_package"] = identifier
    source_url = (server.get("repository") or {}).get("url") or server.get("websiteUrl")
    if source_url:
        entry["source_url"] = source_url
    return entry


def _stdio_fields(package: dict) -> dict | None:
    """本地套件 → hub entry 的傳輸相關欄位；不可無人值守啟動者回 ``None``。"""
    command = _SUPPORTED_REGISTRY_RUNTIMES[package["registryType"]]
    args = _build_args(command, package["identifier"], package)
    if args is None:
        return None
    return {
        "command": command,
        "args": args,
        "env_requirements": [
            ev["name"]
            for ev in package.get("environmentVariables") or []
            if isinstance(ev, dict) and ev.get("isRequired") and ev.get("name")
        ],
    }


def _remote_fields(remotes: list) -> dict | None:
    """遠端端點 → hub entry 的傳輸相關欄位（ADR-0192）；不可用者回 ``None``。

    只收 streamable HTTP 且通過 URL 安全閘者；必填 header 轉成 ``${VAR}`` 模板＋
    對應 ``env_requirements``（憑證實值走 env，沿用既有遮罩保護）。
    """
    for remote in remotes:
        if not isinstance(remote, dict):
            continue
        if remote.get("type") != _SUPPORTED_REMOTE_TYPE:
            continue
        url = remote.get("url")
        if not url:
            continue
        try:
            validate_remote_url(url)
        except ValueError:
            continue
        headers: dict[str, str] = {}
        env_requirements: list[str] = []
        hints: list[str] = []
        for header in remote.get("headers") or []:
            if not isinstance(header, dict) or not header.get("isRequired"):
                continue
            header_name = header.get("name")
            if not header_name:
                continue
            env_name = _header_env_name(header_name)
            headers[header_name] = f"${{{env_name}}}"
            env_requirements.append(env_name)
            if header.get("description"):
                hints.append(f"{env_name}：{header['description']}")
        return {
            "url": url,
            "headers": headers,
            "env_requirements": env_requirements,
            "_hint": "；".join(hints),
        }
    return None


def _append_auth_hint(description: str, hint: str) -> str:
    """把認證格式說明接到服務描述之後（無說明則原樣返回）。"""
    return f"{description}\n\n認證：{hint}" if hint else description
