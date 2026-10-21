"""URL 記錄邊界：只保留 scheme、host 與顯式 port。"""

from urllib.parse import urlsplit


def sanitized_url_origin(value: str) -> str:
    """移除 userinfo、path、query、fragment；無法安全解析時回固定標記。"""
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        if parsed.scheme not in {"http", "https"} or not hostname:
            return "invalid-origin"
        parsed_port = parsed.port
    except (TypeError, ValueError):
        return "invalid-origin"
    display_host = f"[{hostname}]" if ":" in hostname else hostname
    port = f":{parsed_port}" if parsed_port is not None else ""
    return f"{parsed.scheme}://{display_host}{port}"
