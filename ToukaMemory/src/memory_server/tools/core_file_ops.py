"""核心檔案操作 (Core File Operations).

本模組提供受沙盒保護的原子檔案操作。
所有操作必須通過 `_enforce_root_bound` 校驗，確保路徑不跳出指定的根目錄邊界 (Root Bound)。
校驗之後的開檔與走訪都不跟隨連結（``utils.no_follow``）：校驗和開檔之間路上某一段被換成連結，
或走訪時遇到代理人自己建的連結，都不會讀寫到根外面（ADR-0292 決定 12）。
"""

from __future__ import annotations

import errno
import fnmatch
import logging
from pathlib import Path, PurePath, PurePosixPath
from typing import TYPE_CHECKING, Any

from memory_server.utils.no_follow import (
    LINK_NOT_FOLLOWED,
    LINK_REFUSED_MESSAGE,
    LinkRefusedError,
    ensure_no_links,
    is_link,
    iter_regular_files,
    read_text_no_follow,
    remove_no_follow,
    write_text_no_follow,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)


class PathGuardError(Exception):
    """路徑安全守衛異常：當嘗試存取沙盒外的路徑時拋出。"""
    pass


def _enforce_root_bound(
    target_path_str: str, root_bound_str: str, root_is_real: bool = False
) -> Path:
    """強制路徑主權邊界校驗 (SSOT 安全機制).

    確保 target_path 絕對不會逃逸出 root_bound 的範圍。

    Args:
        target_path_str (str): 欲存取的目標路徑（相對或絕對）。
        root_bound_str (str): 獲授權的根目錄邊界（絕對路徑）。
        root_is_real (bool): 呼叫端已查過根本身沒有連結、傳的是解析後的真實路徑（代理人的
            專案）；這裡再解析不同，就是查過之後才換成連結。

    Returns:
        Path: 解析後安全且位於邊界內的實體絕對路徑。

    Raises:
        PathGuardError: 當偵測到路徑逃逸或路徑無效時。
        LinkRefusedError: ``root_is_real`` 時根已經變成連結。
    """
    if not root_bound_str:
        msg = "Empty root bound."
        raise PathGuardError(msg)

    root = Path(root_bound_str).resolve()
    # 查過之後根被換成連結時，解析結果是連結另一端：和呼叫端查過的真實路徑比對（ADR-0292 決定 12）
    if root_is_real and root != Path(root_bound_str).absolute():
        raise LinkRefusedError(
            errno.ELOOP, "Root became a symbolic link or junction", root_bound_str
        )

    if not target_path_str or target_path_str.strip() in [".", "/"]:
        return root

    try:
        target = Path(target_path_str)
        target = (root / target).resolve() if not target.is_absolute() else target.resolve()
    except Exception as e:
        msg = f"Invalid path: {e}"
        raise PathGuardError(msg) from e

    try:
        if not target.is_relative_to(root):
            msg = "Path escape detected."
            raise PathGuardError(msg)
    except ValueError as e:
        msg = "Cross-drive forbidden."
        raise PathGuardError(msg) from e

    return target


def _check_no_links(path: Path, resource_proxy: Any) -> None:
    """路上不能有連結；經 proxy 時讀不到狀態（EACCES）會先權限自癒再查一次."""
    if resource_proxy:
        resource_proxy.ensure_no_links(str(path))
    else:
        ensure_no_links(path)


def _not_a_file(path: Path) -> bool:
    """明顯不是一般檔案（不存在或是資料夾）.

    讀不到狀態時回 False，交給之後的開檔報錯（經 proxy 的開檔會權限自癒）。
    """
    try:
        return not path.is_file()
    except PermissionError:
        return False


def _regular_files(root: Path, skipped: frozenset[str], resource_proxy: Any) -> Iterator[str]:
    """``root`` 底下一般檔案的相對 POSIX 路徑（直接 I/O 時邊走邊給，呼叫端夠了就停）.

    不進入、不列出連結；路徑任一段在 ``skipped`` 就略過。
    """
    _check_no_links(root, resource_proxy)
    if resource_proxy:
        rows = resource_proxy.stat_files(str(root), exclude_names=skipped)
        rels: Iterator[str] = (rel for rel, _size, _mtime in rows)
    else:
        rels = (rel for rel, _path in iter_regular_files(root, skipped))
    for rel in rels:
        if not skipped.intersection(PurePosixPath(rel).parts):
            yield rel


def _glob_match(rel: str, pattern: str) -> bool:
    """``rel`` 是否符合 ``root.rglob(pattern)``：``**`` 比對零到多段，其餘逐段用 fnmatch.

    逐段推進「比到模式第幾段」的集合，時間和段數成正比（模式寫很多 ``**`` 也不會爆量）。
    """
    pattern_parts = ("**", *PurePath(pattern).parts)
    if len(pattern_parts) == 1:
        return False

    def widen(states: set[int]) -> set[int]:
        # ``**`` 可以比對零段：跳過它
        for j in sorted(states):
            while j < len(pattern_parts) and pattern_parts[j] == "**":
                j += 1
                states.add(j)
        return states

    states = widen({0})
    for part in PurePosixPath(rel).parts:
        advanced: set[int] = set()
        for j in states:
            if j == len(pattern_parts):
                continue
            if pattern_parts[j] == "**":
                advanced.add(j)
            elif fnmatch.fnmatch(part, pattern_parts[j]):
                advanced.add(j + 1)
        states = widen(advanced)
        if not states:
            return False
    return len(pattern_parts) in states


async def core_read(
    path: str, 
    root_bound: str, 
    offset: int = 0, 
    limit: int = -1, 
    resource_proxy: Any = None,
    root_is_real: bool = False,
    **_kwargs: Any
) -> str:
    """安全讀取檔案內容.

    Args:
        path (str): 相對路徑。
        root_bound (str): 根目錄邊界。
        offset (int): 起始行數 (0-indexed)。
        limit (int): 讀取行數限制。
        resource_proxy (Any): 注入的 IResourceProxy。
        root_is_real (bool): 見 ``_enforce_root_bound``。
        **_kwargs (Any): 其他參數。

    Returns:
        str: 檔案內容或錯誤訊息。
    """
    try:
        safe_path = _enforce_root_bound(path, root_bound, root_is_real)
        if _not_a_file(safe_path):
            return f"Error: File not found or is a directory: {path}"
        if resource_proxy:
            content_str = resource_proxy.read_file_text_no_follow(str(safe_path))
        else:
            content_str = read_text_no_follow(safe_path, errors='replace')
        lines = content_str.splitlines(True) if content_str else []

        total_lines = len(lines)
        start = max(0, offset)
        end = total_lines if limit == -1 else min(total_lines, start + limit)

        content = "".join(lines[start:end])
        header = f"--- Read {path} (Lines {start+1} to {end} of {total_lines}) ---\n"
    except LinkRefusedError:
        logger.warning("Refused to read %s: the path goes through a link", path)
        return f"Error reading file '{path}': {LINK_REFUSED_MESSAGE}"
    except Exception as e:
        logger.exception("Error reading %s", path)
        return f"Error reading file '{path}': {str(e)}"
    else:
        return header + content


async def core_write(
    path: str, 
    root_bound: str, 
    content: str, 
    mode: str = 'w', 
    resource_proxy: Any = None,
    root_is_real: bool = False,
    **_kwargs: Any
) -> str:
    """安全寫入檔案 (支援新建與追加).

    Args:
        path (str): 相對路徑。
        root_bound (str): 根目錄邊界。
        content (str): 內容。
        mode (str): 'w' (複寫) 或 'a' (追加)。
        resource_proxy (Any): 注入的 IResourceProxy。
        root_is_real (bool): 見 ``_enforce_root_bound``。
        **_kwargs (Any): 其他參數。

    Returns:
        str: 執行結果。
    """
    try:
        safe_path = _enforce_root_bound(path, root_bound, root_is_real)
        if resource_proxy and mode == 'a':
            resource_proxy.append_file_text_no_follow(str(safe_path), content)
        elif resource_proxy:
            resource_proxy.write_file_text_no_follow(str(safe_path), content)
        else:
            write_text_no_follow(safe_path, content, append=(mode == 'a'))

        action = "Appended to" if mode == 'a' else "Wrote to"
    except LinkRefusedError:
        logger.warning("Refused to write %s: the path goes through a link", path)
        return f"Error writing file '{path}': {LINK_REFUSED_MESSAGE}"
    except Exception as e:
        logger.exception("Error writing %s", path)
        return f"Error writing file '{path}': {str(e)}"
    else:
        return f"Success: {action} {path}"


async def core_str_replace(
    path: str, 
    root_bound: str, 
    old_str: str, 
    new_str: str, 
    resource_proxy: Any = None,
    root_is_real: bool = False,
    **_kwargs: Any
) -> str:
    """精確尋找並替換檔案中的首個匹配字串.

    Args:
        path (str): 檔案路徑。
        root_bound (str): 邊界。
        old_str (str): 舊字串。
        new_str (str): 新字串。
        resource_proxy (Any): 注入的 IResourceProxy。
        root_is_real (bool): 見 ``_enforce_root_bound``。
        **_kwargs (Any): 其他參數。

    Returns:
        str: 執行結果。
    """
    try:
        safe_path = _enforce_root_bound(path, root_bound, root_is_real)
        if _not_a_file(safe_path):
            return f"Error: File not found: {path}"
        if resource_proxy:
            content = resource_proxy.read_file_text_no_follow(str(safe_path)) or ""
        else:
            content = read_text_no_follow(safe_path, errors='replace')
        if old_str not in content:
            return f"Error: String '{old_str}' not found in {path}."
        new_content = content.replace(old_str, new_str, 1)
        if resource_proxy:
            resource_proxy.write_file_text_no_follow(str(safe_path), new_content)
        else:
            write_text_no_follow(safe_path, new_content)
    except LinkRefusedError:
        logger.warning("Refused to edit %s: the path goes through a link", path)
        return f"Error replacing in '{path}': {LINK_REFUSED_MESSAGE}"
    except Exception as e:
        logger.exception("Error replacing in %s", path)
        return f"Error replacing in '{path}': {str(e)}"
    else:
        return f"Success: Replaced '{old_str}' in {path}"


async def core_delete(
    path: str, 
    root_bound: str, 
    resource_proxy: Any = None, 
    root_is_real: bool = False,
    **_kwargs: Any
) -> str:
    """安全刪除檔案."""
    try:
        safe_path = _enforce_root_bound(path, root_bound, root_is_real)
        if _not_a_file(safe_path):
            return f"Error: File not found: {path}"
        if resource_proxy:
            resource_proxy.remove_file_no_follow(str(safe_path))
        else:
            remove_no_follow(safe_path)
    except LinkRefusedError:
        logger.warning("Refused to delete %s: the path goes through a link", path)
        return f"Error deleting file '{path}': {LINK_REFUSED_MESSAGE}"
    except Exception as e:
        logger.exception("Error deleting %s", path)
        return f"Error deleting file '{path}': {str(e)}"
    else:
        return f"Success: Deleted {path}"


async def core_list(
    path: str, 
    root_bound: str, 
    max_depth: int = 2, 
    resource_proxy: Any = None, 
    exclude: frozenset[str] = frozenset(),
    root_is_real: bool = False,
    **_kwargs: Any
) -> str:
    """以 Tree 結構列出目錄內容（exclude：額外隱藏的名稱，預設不額外隱藏；連結只列名稱、不進入）."""
    try:
        safe_path = _enforce_root_bound(path, root_bound, root_is_real)
        _check_no_links(safe_path, resource_proxy)

        if resource_proxy and hasattr(resource_proxy, "list_directory"):
            lines = resource_proxy.list_directory(str(safe_path), max_depth, exclude=exclude)
            return "\n".join(lines)
            
        # Fallback raw logic
        if not safe_path.is_dir():
            return f"Error: Directory not found: {path}"

        def build_tree(current: Path, depth: int, prefix: str = "") -> list[str]:
            """遞迴遍歷目錄以建構樹狀結構字串。"""
            if depth > max_depth:
                return [f"{prefix}... (max depth {max_depth} reached)"]
                
            lines = []
            try:
                hidden = {'.git', 'node_modules', '__pycache__', '.pytest_cache', '.ruff_cache'}
                items = sorted([p for p in current.iterdir() if p.name not in hidden | exclude])
                
                for i, item in enumerate(items):
                    is_last = (i == len(items) - 1)
                    connector = "└── " if is_last else "├── "
                    
                    if is_link(item):
                        lines.append(f"{prefix}{connector}{item.name} {LINK_NOT_FOLLOWED}")
                    elif item.is_dir():
                        lines.append(f"{prefix}{connector}{item.name}/")
                        new_prefix = prefix + ("    " if is_last else "│   ")
                        lines.extend(build_tree(item, depth + 1, new_prefix))
                    else:
                        lines.append(f"{prefix}{connector}{item.name}")
                if not items:
                     lines.append(f"{prefix}└── (empty)")
            except PermissionError:
                lines.append(f"{prefix}└── [Permission Denied]")
            return lines

        tree = [f"📁 {path or '/'}:"] + build_tree(safe_path, 1)
        return "\n".join(tree)
        
    except LinkRefusedError:
        return f"Error listing {path}: {LINK_REFUSED_MESSAGE}"
    except Exception as e:
        return f"Error listing {path}: {str(e)}"


async def core_find(
    pattern: str, 
    root_bound: str, 
    resource_proxy: Any = None, 
    exclude: frozenset[str] = frozenset(),
    root_is_real: bool = False,
    **_kwargs: Any
) -> str:
    """按檔名模式在邊界內搜尋檔案（exclude：額外略過的名稱）.

    比對方式同 ``root.rglob(pattern)``；不進入、也不列出連結。
    """
    try:
        root = _enforce_root_bound("", root_bound, root_is_real)
        skipped = frozenset({'.git', 'node_modules', '__pycache__'}) | exclude
        matches = []
        for rel in _regular_files(root, skipped, resource_proxy):
            if _glob_match(rel, pattern):
                matches.append(rel)
                if len(matches) >= 50:
                    break
                    
        if not matches:
            return f"No files found matching '{pattern}'."
             
        res = f"Found {len(matches)}{'+' if len(matches) >= 50 else ''} files:\n"
        res += "\n".join([f"- {m}" for m in matches])
    except LinkRefusedError:
        return f"Error finding files: {LINK_REFUSED_MESSAGE}"
    except Exception as e:
        return f"Error finding files: {str(e)}"
    else:
        return res


async def core_grep(
    query: str, 
    root_bound: str, 
    resource_proxy: Any = None, 
    exclude: frozenset[str] = frozenset(),
    root_is_real: bool = False,
    **_kwargs: Any
) -> str:
    """在所有文本檔案中搜尋關鍵字（exclude：額外略過的名稱；不進入、不讀連結）."""
    try:
        root = _enforce_root_bound("", root_bound, root_is_real)
        matches = []
        limit = 50
        
        exclude_dirs = {'.git', 'node_modules', '__pycache__', '.next', 'out', 'dist', 'build'}
        exclude_dirs |= exclude
        exclude_exts = {
            '.png', '.jpg', '.jpeg', '.gif', '.pdf', '.zip', '.tar', '.gz', 
            '.mp3', '.mp4', '.pyc', '.sqlite3', '.db'
        }

        # 只看一般檔案、不進入連結；讀檔也不跟隨連結（走訪之後才換成連結的檔案讀不到）
        for rel in _regular_files(root, frozenset(exclude_dirs), resource_proxy):
            if len(matches) >= limit:
                break
            if PurePosixPath(rel).suffix.lower() in exclude_exts:
                continue
            try:
                if resource_proxy:
                    content = resource_proxy.read_file_text_no_follow(str(root / rel)) or ""
                else:
                    content = read_text_no_follow(root / rel, errors='ignore')
                for i, line in enumerate(content.splitlines(), 1):
                    if query.lower() in line.lower():
                        snippet = line.strip()
                        if len(snippet) > 150:
                            idx = snippet.lower().find(query.lower())
                            snippet = f"... {snippet[max(0, idx-50):idx+100]} ..."
                        matches.append(f"{rel}:{i}: {snippet}")
                        if len(matches) >= limit:
                            break
            except Exception:
                logger.debug("Could not read %s during grep", rel)
                continue

        if not matches:
            return f"No occurrences of '{query}' found."
             
        res = f"Found {len(matches)}{'+' if len(matches) >= limit else ''} occurrences:\n"
        res += "\n".join(matches)
    except LinkRefusedError:
        return f"Error executing grep: {LINK_REFUSED_MESSAGE}"
    except Exception as e:
        return f"Error executing grep: {str(e)}"
    else:
        return res

