"""協定定義模組 (Protocols).

本模組定義 ToukaMemory 所需的依賴反轉介面 (Dependency Inversion Interfaces)，
確保底層領域邏輯不會強依賴於外部基礎設施 (如 ToukaProxy)。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import os
    from collections.abc import Callable, Iterator, Mapping
    from pathlib import Path
    from typing import IO

    from memory_server.models.conversation_search import ConversationPage
    from memory_server.models.source_ref import SourceRef, SourceStatus
    from memory_server.utils.no_follow import WalkEntry, WalkReport
    from memory_server.utils.zone_paths import ZonePath


class ISourceResolver(Protocol):
    """記憶來源查回介面：Proxy 實作（先查對話樹、再查舊對話庫），由記憶閘道持有.

    只回狀態、不回原文；同時檢查那段對話屬於這個身分。
    """

    async def resolve(self, identity: Any, source: SourceRef) -> SourceStatus:
        """查回一筆來源目前的狀態（原文仍在、已修改、已不在）."""
        ...


class IContinuityEventRecorder(Protocol):
    """可信活動事件的記錄器：Proxy 實作並在啟動時登記（``services.event_recorder``）.

    只在事實已提交後呼叫；同步、不阻塞、執行緒安全，絕不丟例外（寫失敗只記 warning）。
    """

    def record(
        self, identity: Any, kind: str, subject: str, metadata: Mapping[str, str | int | bool]
    ) -> None:
        """記一筆事件（``subject`` 是主體 id，metadata 只放 id、計數與固定列舉值）."""
        ...


class IConversationReader(Protocol):
    """已綁定的 session store reader；身分物件由執行層注入。"""

    async def query_ui_conversations(
        self,
        identity: Any,
        query: str,
        role: str | None,
        date_start: int | None,
        date_end: int | None,
        oldest_first: bool,
        limit: int,
        offset: int,
    ) -> ConversationPage:
        """同一讀取交易內驗歸屬、計數並切頁；失敗直接往上拋。"""
        ...


class IResourceProxy(IConversationReader, Protocol):
    """全能 I/O 代理介面協定 (Full I/O Proxy Protocol).

    定義了外部代理程式（例如 ToukaProxy 的 ResourceManager）必須提供的方法。
    ToukaMemory 將依賴此協定而非具體實作，以確保 MCP 獨立性。
    """

    def read_file_text(self, path: Any, encoding: str = "utf-8") -> str:
        """讀取文字檔案內容."""
        ...

    def read_file_binary(self, path: Any) -> bytes:
        """讀取二進位檔案內容."""
        ...

    def write_file_text(
        self, path: Any, content: str, encoding: str = "utf-8", make_parents: bool = True
    ) -> None:
        """寫入文字檔案內容."""
        ...

    def append_file_text(self, path: Any, content: str, encoding: str = "utf-8") -> None:
        """附加文字到檔案末尾."""
        ...

    def exists(self, path: Any) -> bool:
        """檢查路徑是否存在."""
        ...

    def make_dir(self, path: Any, parents: bool = True, exist_ok: bool = True) -> None:
        """建立目錄."""
        ...

    def remove_file(self, path: Any, missing_ok: bool = True) -> None:
        """刪除檔案."""
        ...

    def move_file(self, src: Any, dst: Any) -> None:
        """移動或改名檔案（目的地已存在時直接覆蓋）."""
        ...

    def iter_files(self, path: Any) -> list[Path]:
        """遞迴列出目錄下所有檔案的絕對路徑（無數量上限）."""
        ...

    def list_directory(
        self, path: Any, max_depth: int = 2, exclude: frozenset[str] = frozenset()
    ) -> list[str]:
        """列出目錄結構樹（exclude：額外隱藏的名稱；連結只列名稱、不進入）."""
        ...

    # 不跟隨連結（代理人的檔案動詞用）：路上任何一段是 symlink 或 junction 就丟 LinkRefusedError
    def ensure_no_links(self, path: Any) -> None:
        """路上不能有連結（讀不到狀態時先權限自癒）."""
        ...

    def read_file_text_no_follow(self, path: Any, encoding: str = "utf-8") -> str:
        """讀文字檔，不跟隨連結."""
        ...

    def read_file_binary_no_follow(self, path: Any) -> bytes:
        """讀二進位檔，不跟隨連結."""
        ...

    def write_file_text_no_follow(self, path: Any, content: str, encoding: str = "utf-8") -> None:
        """原子寫入文字檔（受保護路徑拒絕），不跟隨連結."""
        ...

    def append_file_text_no_follow(self, path: Any, content: str, encoding: str = "utf-8") -> None:
        """追加文字（受保護路徑拒絕），不跟隨連結."""
        ...

    def remove_file_no_follow(self, path: Any) -> None:
        """刪除檔案（受保護路徑拒絕），不跟隨連結."""
        ...

    def glob_search(
        self, path: Any, pattern: str, exclude: frozenset[str] = frozenset()
    ) -> list[str]:
        """按檔名模式在邊界內搜尋檔案（exclude：額外略過的路徑段名稱）."""
        ...

    def stat_files(
        self,
        path: Any,
        exclude_names: frozenset[str] = frozenset(),
        exclude_paths: frozenset[str] = frozenset(),
        exclude_globs: tuple[str, ...] = (),
        max_files: int | None = None,
    ) -> list[tuple[str, int, int]]:
        """列出一般檔案的（相對 POSIX 路徑, 位元組數, mtime_ns）；不跟隨連結."""
        ...

    def is_protected_path(self, path: Any) -> bool:
        """檢查是否為受保護路徑."""
        ...

    def get_vault_path(self, vault: str, identity: Any) -> str | None:
        """取得指定代理人的 Vault 實體絕對路徑."""
        ...

    # 代理人資料夾（區）：只收 ZonePath，路上或最後一段有連結丟 ZoneLinkRefusedError（ADR-0293）
    def agent_drive(self, identity: Any) -> ZonePath | None:
        """代理人的個人空間；``agent_id`` 不能當一段時回 None."""
        ...

    def agent_drive_for(self, agent_id: str) -> ZonePath | None:  # noqa: IDENTITY_SSOT
        """依 agent_id 找出真實註冊身分後的個人空間（跨身分決定論定址，不臆測角色）."""
        ...

    def outputs_dir(self, identity: Any) -> ZonePath | None:
        """代理人的輸出資料夾."""
        ...

    def zone_rel_from_abs(self, base: ZonePath, text: str) -> ZonePath | None:
        """絕對路徑照字面在 ``base`` 底下時回區內路徑，否則 None."""
        ...

    def zone_kind(self, zp: ZonePath) -> str:
        """最後一段是什麼：``missing``／``file``／``dir``／``link``／``other``."""
        ...

    def zone_read_text(self, zp: ZonePath, encoding: str = "utf-8", errors: str = "strict") -> str:
        """讀文字檔."""
        ...

    def zone_read_binary(self, zp: ZonePath, size: int = -1) -> bytes:
        """讀二進位檔（``size`` 給了只讀開頭）."""
        ...

    def zone_read_json(self, zp: ZonePath, encoding: str = "utf-8") -> Any | None:
        """讀 JSON；不存在或壞檔回 None."""
        ...

    def zone_write_text(self, zp: ZonePath, content: str, encoding: str = "utf-8") -> None:
        """原子寫入文字檔."""
        ...

    def zone_write_json(self, zp: ZonePath, data: Any, indent: int = 4) -> None:
        """寫成 JSON."""
        ...

    def zone_write_binary(self, zp: ZonePath, content: bytes) -> None:
        """就地寫入二進位檔."""
        ...

    def zone_append_text(self, zp: ZonePath, content: str, encoding: str = "utf-8") -> None:
        """追加文字."""
        ...

    def zone_make_dir(self, zp: ZonePath) -> None:
        """建資料夾（含上層）."""
        ...

    def zone_remove_file(self, zp: ZonePath, missing_ok: bool = True) -> None:
        """刪檔（最後一段是連結時刪連結本身）."""
        ...

    def zone_remove_dir(self, zp: ZonePath, missing_ok: bool = True) -> None:
        """刪整棵樹（連結只刪本身）."""
        ...

    def zone_list(
        self, zp: ZonePath, max_depth: int = 2, exclude: frozenset[str] = frozenset()
    ) -> list[str]:
        """同 ``list_directory`` 的樹狀格式."""
        ...

    def zone_stat_files(
        self,
        zp: ZonePath,
        exclude_names: frozenset[str] = frozenset(),
        exclude_paths: frozenset[str] = frozenset(),
        exclude_globs: tuple[str, ...] = (),
        max_files: int | None = None,
    ) -> list[tuple[str, int, int]]:
        """同 ``stat_files``."""
        ...

    def zone_walk(
        self,
        zp: ZonePath,
        report: WalkReport,
        *,
        skip_dir: Callable[[tuple[str, ...]], bool] | None = None,
    ) -> Iterator[WalkEntry]:
        """走訪一棵樹（不進入連結）."""
        ...

    def zone_walk_open(
        self, zp: ZonePath, report: WalkReport
    ) -> Iterator[tuple[tuple[str, ...], IO[bytes], os.stat_result]]:
        """走訪並給開好的一般檔案."""
        ...
