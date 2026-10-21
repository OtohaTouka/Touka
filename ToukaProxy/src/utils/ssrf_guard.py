# ruff: noqa: TRY003  # UnsafeUrlError 的描述性訊息於 raise 處構造（小模組、訊息即文件）
"""SSRF 防護 (Outbound URL Guard) —— 對「使用者/管理者可設定的對外請求 URL」做最小必要驗證。

Touka 為自架、單一擁有者、連線需 Owner/Debugger/admin 角色的系統；使用者**合法地**會把連線
指向自架/區網的 LLM 端點（Ollama localhost:11434、LiteLLM localhost:4000、區網伺服器、容器
DNS 名稱）。因此本守衛**刻意不封鎖 loopback/私網**——那會破壞核心自架情境——而是封鎖在此
威脅模型下「絕無正當用途、卻是 SSRF 主要標的」的目標：

  - 雲端 metadata / link-local（IPv4 169.254.0.0/16 含 169.254.169.254、IPv6 fe80::/10）
    —— AWS/GCP/Azure 憑證竊取的經典標的。
  - 未指定位址（0.0.0.0、::）、多播、保留、IPv4 廣播。
  - 非 http/https scheme（file://、gopher:// 等協定走私）。

並要求呼叫端維持 `follow_redirects=False`（避免先過驗證、再被 3xx 導去 metadata）。

涵蓋的繞過手法：IPv4-mapped IPv6（`::ffff:169.254.169.254`）、以及 OS resolver 接受的各種
舊式 IPv4 編碼——十進位整數（`2852039166`）、十六進位（`0xa9.0xfe.0xa9.0xfe`）、八進位
（`0251.0376...`）、dword（`0x7f000001`）——皆以 `socket.inet_aton` 正規化後再判斷網段，
避免以非點分形式繞過。並拒絕內嵌帳密（userinfo）的 URL（避免金鑰隨網址外洩進日誌）。

殘餘風險（在此威脅模型下接受）：以「可解析到 metadata 的網域名稱」繞過——本守衛不在驗證時
做 DNS 解析（避免延遲、TOCTOU、且容器 DNS 僅網內可解析）；因連線為 admin 設定 ＋ 已封鎖
redirect 而風險低。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

_ALLOWED_SCHEMES = frozenset({"http", "https"})

# 明確放行的私網網段（RFC1918 ＋ IPv6 ULA）——自架/區網 LLM 端點的合法目標。
# loopback 與全球可路由公網另以 is_loopback / is_global 放行。其餘一律封鎖。
_PRIVATE_ALLOW_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)


class UnsafeUrlError(ValueError):
    """對外請求 URL 未通過 SSRF 守衛（scheme 不符或指向受封鎖網段）。"""


def _coerce_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """將主機字串轉為 IP 物件；非 IP 字面值（網域名稱）回 None。

    先以 `ipaddress` 解析標準點分 IPv4/IPv6；失敗則以 `socket.inet_aton` 嘗試 OS resolver
    接受的舊式 IPv4 編碼（十六進位/八進位/十進位整數/dword），正規化後再判斷，避免繞過。
    `inet_aton` 對真正的網域名稱會丟 OSError → 回 None（視為網域名稱、放行）。
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except (OSError, ValueError):
        return None


def _block_reason(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """回傳封鎖原因；放行則回 None（採白名單：loopback／RFC1918+ULA 私網／全球公網）。

    用白名單而非黑名單，是因 Python `is_private` 過廣（含 240/4、255.255.255.255 等），
    無法直接當作「放行私網」的判準。明確放行三類合法目標，其餘（link-local/metadata、
    未指定、多播、保留、廣播…）一律封鎖。
    """
    # IPv4-mapped IPv6（::ffff:a.b.c.d）→ 取出內嵌 IPv4 再判斷，避免繞過
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    # 明確放行：loopback ＋ RFC1918/ULA 私網（自架/區網合法目標）
    if ip.is_loopback:
        return None
    if any(ip.version == net.version and ip in net for net in _PRIVATE_ALLOW_NETS):
        return None
    # 封鎖已知不良類別（須在 is_global 之前——多播等在 Python 中 is_global 可能為 True）
    if ip.is_multicast:
        return "多播位址"
    if ip.is_link_local:
        return "link-local/雲端 metadata 位址"
    if ip.is_unspecified:
        return "未指定位址"
    if ip.is_reserved:
        return "保留位址"
    # 全球可路由公網 → 放行；其餘（廣播等特殊用途）→ 封鎖
    if ip.is_global:
        return None
    return "特殊用途（非公開、非私網）位址"


def validate_outbound_url(url: str) -> None:
    """驗證對外請求 URL；不安全則 raise UnsafeUrlError。

    放行 loopback/私網/公網的 http(s) URL；封鎖 link-local/metadata、未指定、多播、保留、
    廣播位址與非 http(s) scheme。host 為網域名稱時放行（不做 DNS 解析，見模組殘餘風險）。
    """
    if not url or not url.strip():
        raise UnsafeUrlError("URL 為空")
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise UnsafeUrlError(
            f"不支援的協定：{parts.scheme!r}（僅允許 http/https）"
        )
    # 拒絕內嵌帳密的 URL（user:pass@host）：金鑰應走 apiKey 欄位，內嵌易隨網址外洩進日誌
    if parts.username or parts.password:
        raise UnsafeUrlError("URL 不得內嵌帳密（userinfo）；請改用 apiKey 欄位")
    host = parts.hostname
    if not host:
        raise UnsafeUrlError("URL 缺少主機名")
    ip = _coerce_ip(host)
    if ip is None:
        return  # 網域名稱 → 放行
    reason = _block_reason(ip)
    if reason:
        raise UnsafeUrlError(f"封鎖對{reason}的對外請求：{host}")


def is_safe_outbound_url(url: str) -> bool:
    """布林版本（不拋例外）。"""
    try:
        validate_outbound_url(url)
    except UnsafeUrlError:
        return False
    else:
        return True
