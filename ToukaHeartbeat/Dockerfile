# ADR-0117：依賴改由 uv workspace 單一 uv.lock 佈建（root build context——compose 設
# context: . ＋ dockerfile: ToukaHeartbeat/Dockerfile）。映像佈局刻意維持 /app＝本子專案
# 內容（compose 掛載 /app/awaken.py＋設定『目錄』/app/config——ADR-0150 改掛目錄而非
# 舊的單檔 /app/config.json）。context 過濾見同目錄
# Dockerfile.dockerignore（BuildKit per-Dockerfile ignore：本 build 完全取代根 .dockerignore）。
#
# Multi-stage：uv 只存在於建置階段（~40MB 不進 runtime 映像；runtime 不含 uv/Node）。
# venv 於兩階段維持同一絕對路徑 /ws/.venv（venv 不可搬移；python symlink 指向同款基底）。

# ── 建置階段：解析／安裝鎖定依賴 ────────────────────────────────────────────
FROM python:3.12-slim-trixie@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea AS deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash ca-certificates libstdc++6 && rm -rf /var/lib/apt/lists/*
# uv 0.12.1 linux/amd64（與 ToukaProxy/Dockerfile 同一 digest）。
COPY --from=ghcr.io/astral-sh/uv@sha256:f6edac8c6d516a01a5387a8655c97481e0db46dd0a62ab62f41e5b2294376f6e /uv /uvx /bin/

ENV UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1

# 依賴：workspace 描述檔 → 只裝本成員鎖定子集（精簡映像：無 torch/transformers）。
# --locked：鎖檔與 pyproject 不一致即 fail-loud；--no-dev：測試工具不進生產映像。
WORKDIR /ws
COPY pyproject.toml uv.lock ./
COPY ToukaProxy/pyproject.toml ToukaProxy/
COPY ToukaMemory/pyproject.toml ToukaMemory/
COPY ToukaHeartbeat/pyproject.toml ToukaHeartbeat/
COPY ToukaDispatch/pyproject.toml ToukaDispatch/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --package toukaheartbeat

# ── Runtime 階段 ─────────────────────────────────────────────────────────────
# Debian 套件以 apt-get upgrade 取官方安全更新。
FROM python:3.12-slim-trixie@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    bash ca-certificates libstdc++6 && rm -rf /var/lib/apt/lists/*

ARG TOUKA_SOURCE_REVISION
LABEL org.opencontainers.image.title="touka-heartbeat" \
    org.opencontainers.image.revision="${TOUKA_SOURCE_REVISION}" \
    org.opencontainers.image.source="https://github.com/OtohaTouka/Touka"

# P8 fix：非 root 使用者（shadow useradd，SSOT 對齊 ToukaProxy；先建好供 COPY --chown 引用）
RUN groupadd -g 1001 appuser && useradd -r -u 1001 -g 1001 -m appuser

# venv 原路徑搬入（root 擁有＝appuser 只讀執行，與「系統 site-packages」等價；
# 嚴禁事後 chown -R——overlayfs 會把 venv 重複進第二個 layer 造成映像膨脹）
COPY --from=deps /ws/.venv /ws/.venv
ENV PATH="/ws/.venv/bin:$PATH"

WORKDIR /app

# 複製本子專案完整源碼（整目錄複製、非逐檔挑選——物理完整性同 COPY . .，Guard #16 認可）
COPY --chown=appuser:appuser ToukaHeartbeat/ .

EXPOSE 8082

# /app 目錄本身給 appuser（非遞迴、單筆 metadata）：執行期可寫 config.json 等
RUN chown appuser:appuser /app
USER appuser

# 執行 FastAPI 伺服器
CMD ["python", "awaken.py"]
