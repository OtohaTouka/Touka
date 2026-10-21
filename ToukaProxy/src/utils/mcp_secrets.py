"""MCP 傳輸與診斷共用的環境值遮罩。"""

import json
import re
from collections.abc import Iterable, Mapping

REDACT_MIN_CHARS = 4
REDACTED = "<redacted>"
PATH_MASK = "<path>"
DIAGNOSTIC_SCAN_CHARS = 2000
DIAGNOSTIC_DISPLAY_CHARS = 300

# 健康診斷錯誤訊息脫敏（會回傳前端）：去絕對路徑、遮罩疑似機密 token，杜絕洩漏金鑰/路徑。
# 註：精準防線是 `ProtocolManager._sanitize_for`（逐一遮罩該伺服器 env 的實際值，任意金鑰格式皆中、
# 零誤殺）；以下正則為通用 backstop（涵蓋 UNC/Win/Unix 路徑與常見 token 前綴）。
# \\server\share\...（無磁碟代號，必須先於 Windows 規則）。
_UNC_PATH_RE = re.compile(r"\\\\[^\s\"']+(?:\\[^\s\"']+)*")
_WIN_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"']*")
_UNIX_PATH_RE = re.compile(r"(?<!\w)/(?:[^\s/\"']+/)+[^\s/\"']*")
_SECRET_RE = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{6,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{16,}"
    r"|[A-Za-z0-9_-]{32,}|[A-Fa-f0-9]{24,})"
)
# 網址先切出來另外處理：舊規則把 http:// 的 p:/ 當磁碟代號、把網址路徑當 Unix 路徑。
# 網址結尾和 Win 規則相同（空白或引號）；舊 Unix 規則會借網址 :// 的斜線遮住緊貼的單段
# 路徑，所以緊貼網址的片段補一個哨兵斜線再跑 Unix 規則。
# http(s) 只顯示合法主機、埠與白名單路徑段，碰到第一個不符的段或 ?、# 就把剩下的
# 換成一個 <redacted>。file: 的路徑就是本機路徑，其他 scheme 語意不明，整段遮。
_URL_RE = re.compile(r"(?<![A-Za-z0-9_+.-])[A-Za-z][A-Za-z0-9+.-]*://[^\s\"']*")
_URL_TRAILING_PUNCTUATION = ".,;:!?)}"
_READABLE_URL_SCHEMES = frozenset({"http", "https"})
_NAMED_URL_SCHEMES = frozenset({"file", "ws", "wss"})
_URL_HOST_RE = re.compile(
    r"(?:(?:[A-Za-z0-9-]{1,63}\.)*[A-Za-z0-9-]{1,63}|\[[0-9A-Fa-f:.]{2,45}\])(?::[0-9]{1,5})?"
)
_URL_SEGMENT_RE = re.compile(r"/([^/?#]*)")
_URL_WORD_SEGMENT_RE = re.compile(r"[a-z0-9][a-z0-9._~-]{0,14}")
# 8 字以上又含數字的段多半是 token；本機根目錄名多半是回顯的本機路徑。
URL_DIGIT_SEGMENT_MAX_CHARS = 7
_LOCAL_ROOT_NAMES = frozenset(
    {"home", "root", "tmp", "etc", "var", "usr", "app", "mnt", "opt", "srv", "proc", "private"}
)


def escape_log_controls(text: str) -> str:
    """不可信 stderr 不得改寫終端行或文字方向；tab 可保留。"""
    return "".join(
        (f"\\x{ord(char):02x}" if ord(char) <= 0xff else f"\\u{ord(char):04x}")
        if (ord(char) < 32 and char != "\t" or 127 <= ord(char) <= 159
            or ord(char) in {0x061c, 0x200e, 0x200f, 0x2028, 0x2029}
            or 0x202a <= ord(char) <= 0x202e or 0x2066 <= ord(char) <= 0x2069)
        else char for char in text
    )


def secret_values(env: Mapping[str, str]) -> tuple[str, ...]:
    """工具網址是公開診斷資訊，不是憑證。"""
    return tuple(value for key, value in env.items() if key != "TOOL_BASE_URL")


def secret_parts(values: Iterable[str]) -> tuple[str, ...]:
    """伺服器可能只印一行，或用 Python／JSON 跳脫後回顯。"""
    parts = {part for value in values for part in (value, *value.splitlines())}
    return tuple({
        form for p in parts if len(p) >= REDACT_MIN_CHARS
        for form in (p, repr(p)[1:-1], json.dumps(p)[1:-1])
    })


def redact_values[T: (str, bytes)](text: T, values: Iterable[str]) -> T:
    """在原文合併所有命中區間，避免替換後破壞重疊值的比對。"""
    intervals: list[tuple[int, int]] = []
    for value in secret_parts(values):
        part = value.encode("utf-8", "surrogatepass") if isinstance(text, bytes) else value
        start = text.find(part)
        while start != -1:
            intervals.append((start, start + len(part)))
            start = text.find(part, start + 1)
    replacement = REDACTED.encode() if isinstance(text, bytes) else REDACTED
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    chunks: list[T] = []
    cursor = 0
    for start, end in merged:
        chunks.extend((text[cursor:start], replacement))
        cursor = end
    chunks.append(text[cursor:])
    return text[:0].join(chunks)


def sanitize_diagnostic(msg: str) -> str:
    """脫敏 MCP 啟動/連線錯誤訊息：去絕對路徑、遮罩疑似機密、截斷長度（供回傳前端、不洩漏）。

    先以較寬的上限跑替換，確保位置較後的機密/路徑不因預先截斷成碎片而漏網；
    替換完成後才截到顯示長度。只用 regex 與字串操作，任何輸入都不丟例外（呼叫端在例外處理裡）。
    """
    raw = msg or ""
    text = raw[:DIAGNOSTIC_SCAN_CHARS]
    chunks: list[str] = []
    cursor = 0
    for match in _URL_RE.finditer(text):
        url = match.group()
        body = url.rstrip(_URL_TRAILING_PUNCTUATION)
        cut = match.end() == len(text) < len(raw)  # 截斷切在網址中間：前段可能像主機加埠
        chunks += [
            _mask_local_paths(text[cursor : match.start()], before_url=True),
            REDACTED if cut else _sanitize_url(body) + url[len(body) :],
        ]
        cursor = match.end()
    chunks.append(_mask_local_paths(text[cursor:]))
    return _SECRET_RE.sub(REDACTED, "".join(chunks)).strip()[:DIAGNOSTIC_DISPLAY_CHARS]


def _mask_local_paths(text: str, *, before_url: bool = False) -> str:
    text = _WIN_PATH_RE.sub(PATH_MASK, _UNC_PATH_RE.sub(PATH_MASK, text))
    # 哨兵只給 Unix 規則（Win 規則要求冒號後緊接斜線，舊版本來就借不到網址的斜線）；
    # 哨兵還在就表示沒被吃進路徑，拿掉。
    probe = _UNIX_PATH_RE.sub(PATH_MASK, text + "/" if before_url else text)
    return probe[:-1] if before_url and probe.endswith("/") else probe


def _sanitize_url(url: str) -> str:
    scheme, _, rest = url.partition("://")
    if scheme.lower() not in _READABLE_URL_SCHEMES:
        return f"{scheme}://{PATH_MASK}" if scheme.lower() in _NAMED_URL_SCHEMES else PATH_MASK
    host_end = next((i for i, char in enumerate(rest) if char in "/?#"), len(rest))
    host, tail = rest[:host_end], rest[host_end:]
    if not _URL_HOST_RE.fullmatch(host) or host.partition(":")[0].lower() in _LOCAL_ROOT_NAMES:
        return f"{scheme}://{REDACTED}"
    shown = 0
    while (segment := _URL_SEGMENT_RE.match(tail, shown)) and _url_segment_shown(segment[1]):
        shown = segment.end()
    hidden = f"{tail[shown]}{REDACTED}" if shown < len(tail) else ""
    return f"{scheme}://{host}{tail[:shown]}{hidden}"


def _url_segment_shown(segment: str) -> bool:
    return segment == "" or (
        _URL_WORD_SEGMENT_RE.fullmatch(segment) is not None
        and segment not in _LOCAL_ROOT_NAMES
        and (len(segment) <= URL_DIGIT_SEGMENT_MAX_CHARS or not any(c.isdigit() for c in segment))
    )
