# ADR-0082：單一映像＝ToukaProxy ＋ ToukaMemory ＋ ToukaDispatch。
# build context＝repo 根（compose 設 context: . ＋ dockerfile: ToukaProxy/Dockerfile），
# 黑名單見根 .dockerignore（禁令 47：COPY . . ＋黑名單——密鑰/活體 config/data 絕不入映像）。
# Node／npm 直接取自官方 node 映像（v24.22.1，digest 釘版）；Debian 套件以 apt-get upgrade 取官方安全更新。
# uv 0.12.1（與 CI setup-uv 同版）linux/amd64 子 manifest digest；讀得懂 exclude-newer 期間與 uv.lock 的 exclude-newer-span。
FROM node@sha256:43b9ba1142b8dcf33fa8f922ca1be85714adc444e0586c7cf7726daaeabc1bea AS node-runtime
FROM ghcr.io/astral-sh/uv@sha256:f6edac8c6d516a01a5387a8655c97481e0db46dd0a62ab62f41e5b2294376f6e AS uv-runtime

FROM python:3.12-slim-trixie@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    bash ca-certificates libstdc++6 && rm -rf /var/lib/apt/lists/*

COPY --from=uv-runtime /uv /uvx /bin/
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \
    ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx && \
    test "$(node --version)" = 'v24.22.1' && npm --version && uv --version

RUN groupadd -g 1001 appuser && useradd -r -u 1001 -g 1001 -m appuser

# ── 依賴：uv workspace 單一鎖檔（ADR-0117；取代「pip 單獨裝 torch ＋ pip install -r」）──
# 先只複製 workspace 依賴描述檔以保 Docker layer cache（源碼變動不重裝依賴）。
# torch 由 root pyproject 的 [tool.uv.sources] 綁 pytorch-cpu index → 鎖檔即 CPU 版、零 CUDA
#（映像維持 ~2GB 而非 ~12GB；GPU 需求請於部署時覆寫）。
ENV UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY ToukaProxy/pyproject.toml ToukaProxy/
COPY ToukaMemory/pyproject.toml ToukaMemory/
COPY ToukaHeartbeat/pyproject.toml ToukaHeartbeat/
COPY ToukaDispatch/pyproject.toml ToukaDispatch/
# --locked：鎖檔與 pyproject 不一致即 fail-loud（建置期擋「改依賴忘了 uv lock」）；
# --no-dev：測試工具（dev group）不進生產映像；
# --package：整合映像安裝四個產品 workspace 成員。Heartbeat 仍列入以免引入
#   「Proxy 必為其相依超集」的不變量。
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev \
      --package toukaproxy \
      --package toukamemory \
      --package toukaheartbeat \
      --package toukadispatch

# venv 進 PATH：CMD 的 uvicorn 與 MCP／子進程的 python 均解析至 /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# iso-c2：代理人指令（在執行器容器裡）要用 git。另成一層、放在 uv sync 之後：
# 改這一層不會讓 venv 那層的快取失效。
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

# 複製整個 repo 進 /app（COPY . . ＋根 .dockerignore，物理完整性）：
# ToukaProxy/ → /app/ToukaProxy、ToukaMemory/ → /app/ToukaMemory、ToukaDispatch/ → /app/ToukaDispatch
# --chown 於複製時一次到位——嚴禁事後 `chown -R /app`：overlayfs 會把整個 /app/.venv
#（~2GB）重複進第二個 layer（實測映像 2.66GB → 4.5GB 的回歸根因）。
COPY --chown=appuser:appuser . .
WORKDIR /app/ToukaProxy

# iso-c2：隔離啟動器的唯讀信任根。這是「另加」的一段，不取代上面的 COPY . .（禁令 47 照舊）；
# root 擁有、所有人唯讀，appuser 改不到。開發版掛的 live src 和這份雜湊不同時判 kernel_outdated。
COPY ToukaProxy/src/sandbox_kernel/ /opt/touka-sandbox/
RUN chmod -R a=rX /opt/touka-sandbox
ENV TOUKA_SANDBOX_KERNEL_DIR=/opt/touka-sandbox

# 安裝根目錄不留開發用設定檔（pyproject.toml／uv.lock 只供上面的 uv sync，其餘由 .dockerignore 擋）：
# 隔離後的代理人從自己的資料夾往上找不到任何設定檔，pytest 等工具不會讀到 Touka 的設定。
RUN rm -f /app/pyproject.toml /app/uv.lock

# 整合式 ToukaMemory：映像自帶預設 PYTHONPATH（docker run 免 compose 也能跑；compose 可覆寫）
ENV PYTHONPATH=/app/ToukaProxy:/app/ToukaMemory:/app/ToukaMemory/src

# /app 目錄本身給 appuser（非遞迴、單筆 metadata）：執行期可於 /app 下建新目錄（如 data/）。
# venv 維持 root 擁有＝appuser 只讀執行——與舊映像「系統 site-packages root 擁有」等價。
# sandbox state（named volume 的掛載點）預建成 appuser、0700：空 volume 第一次掛載時照映像複製擁有者。
# 執行器的 ipc 與 state 同理（伺服器要求兩者都是自己的、0700，否則不服務）；/app/ipc 本身是 root 擁有。
RUN chown appuser:appuser /app && install -d -o appuser -g appuser -m 0700 /app/sandbox_state \
    /app/ipc/executor /app/executor_state

# 暴露 FastAPI 預設埠（dispatch 8083 是否對外由 DISPATCH_BIND_HOST 與 compose 發布決定）
EXPOSE 8000

ARG TOUKA_SOURCE_REVISION
LABEL org.opencontainers.image.title="touka-proxy" \
    org.opencontainers.image.revision="${TOUKA_SOURCE_REVISION}" \
    org.opencontainers.image.source="https://github.com/OtohaTouka/Touka"

USER appuser

# 啟動應用程式（DispatchSupervisor 於 lifespan 依 DISPATCH_AUTOSPAWN spawn dispatch 子程序）
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
