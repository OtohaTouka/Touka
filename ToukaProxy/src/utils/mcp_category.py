"""MCP Hub 功能性分類 taxonomy（SSOT）＋ classifier 純函式。

分類頁籤修復：前端 McpHubView 的頁籤分類（Browser/Dev/File/Database/Utils）是
使用者面對的 taxonomy；本模組為其**唯一事實來源**，並提供把任意 hub server 映射到
該 taxonomy 的純函式。``MCPHubService`` 同步時據此標記 ``category``，前端再由 hub
資料**衍生**頁籤（不硬編），從根本杜絕前後端分類漂移（原 bug：後端只給
Official/Community、前端頁籤卻是 Browser/Dev/...，導致除「全部」外永遠空白）。

設計：純函式、零 IO/時間/環境依賴；以 word-boundary 比對避免子字串誤命中
（如 'digital' 誤命中 'git'）；未命中一律落 ``Utils`` catch-all，故**恆回傳合法分類**。
"""
from __future__ import annotations

import re

# 前端頁籤 taxonomy 的唯一事實來源（順序亦即關鍵字比對優先序，末項 Utils 為 catch-all）。
MCP_HUB_CATEGORIES: tuple[str, ...] = ("Browser", "Dev", "File", "Database", "Utils")

# 各分類關鍵字（word-boundary 比對）。優先序：Database → File → Browser → Dev；
# 重疊時前者勝（如 'supabase' 同時像 DB/Dev，歸 Database）。皆未命中 → Utils。
_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Database",
        (
            "database", "db", "sql", "postgres", "postgresql", "mysql", "mongo",
            "mongodb", "redis", "supabase", "sqlite", "duckdb", "snowflake",
            "bigquery", "clickhouse", "qdrant", "pinecone", "elasticsearch",
            "neo4j", "prisma", "airtable",
        ),
    ),
    (
        "File",
        (
            "file", "files", "filesystem", "drive", "dropbox", "storage", "s3",
            "sheet", "sheets", "spreadsheet", "document", "documents", "docs",
            "folder", "pdf",
        ),
    ),
    (
        "Browser",
        (
            "browser", "web", "fetch", "search", "scrape", "scraper", "crawl",
            "crawler", "http", "url", "puppeteer", "playwright", "selenium",
            "reddit", "instagram", "twitter", "youtube", "brave", "exa",
        ),
    ),
    (
        "Dev",
        (
            "git", "github", "gitlab", "code", "coding", "docker", "kubernetes",
            "k8s", "devops", "sdk", "compiler", "terminal", "npm", "sentry",
            "vercel", "debug", "debugger",
        ),
    ),
)

# 模組載入時一次預編譯 word-boundary patterns（不可變、無副作用）。
_COMPILED: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = tuple(
    (category, tuple(re.compile(rf"\b{re.escape(kw)}\b") for kw in keywords))
    for category, keywords in _CATEGORY_KEYWORDS
)


def classify_mcp_category(name: str, description: str = "", npm_package: str = "") -> str:
    """把一個 hub server 映射到 ``MCP_HUB_CATEGORIES`` 之一（純函式，恆回傳合法分類）。

    依 name/description/npm_package 合併文字做 word-boundary 關鍵字比對，按
    Database → File → Browser → Dev 優先序取首個命中者；皆未命中 → ``Utils``。
    """
    text = f"{name} {description} {npm_package}".lower()
    for category, patterns in _COMPILED:
        if any(p.search(text) for p in patterns):
            return category
    return "Utils"
