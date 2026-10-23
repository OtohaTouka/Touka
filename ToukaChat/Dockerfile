# ToukaChat 開發用 Dockerfile（npm run dev，支援 Hot Reload）。
# Node／npm 取自官方 node 映像（v24.22.1，digest 釘版）；Python 基底提供 node-gyp 需要的 python3、make、g++。
FROM node@sha256:43b9ba1142b8dcf33fa8f922ca1be85714adc444e0586c7cf7726daaeabc1bea AS node-runtime
FROM python:3.12-slim-trixie@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    bash ca-certificates libstdc++6 make g++ && rm -rf /var/lib/apt/lists/*
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \
    ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx && \
    test "$(node --version)" = 'v24.22.1'
# package.json 以 engine-strict 鎖定 npm 11.19.1；映像自帶版本不保證相同，明確安裝。
RUN npm install -g --ignore-scripts --no-audit --no-fund npm@11.19.1 && test "$(npm --version)" = '11.19.1'

WORKDIR /app

COPY package*.json ./
COPY .npmrc ./
COPY container-security/braces-backport/backport.mjs container-security/braces-backport/backport.mjs
COPY container-security/braces-backport/manifest.json container-security/braces-backport/manifest.json
COPY container-security/braces-backport/braces-3.0.3-depth-limit.patch container-security/braces-backport/braces-3.0.3-depth-limit.patch
RUN npm ci --ignore-scripts --no-audit --no-fund
RUN node container-security/braces-backport/backport.mjs apply --project-root .
RUN node container-security/braces-backport/backport.mjs verify --project-root . --scope full
# Dev terminal runtime needs the lockfile-bound node-pty native binary. Keep
# global lifecycle disabled and explicitly allow only this package's build.
RUN npm rebuild node-pty --ignore-scripts=false --foreground-scripts --no-audit --no-fund

ENV NEXT_TELEMETRY_DISABLED=1

# P8 fix: run as non-root user
RUN groupadd -g 1001 appuser && useradd -r -u 1001 -g 1001 -m appuser
RUN chown -R appuser:appuser /app
USER appuser

# 使用開發模式啟動，支援 Hot Reload（Next.js dev 跑在 port 3001）
EXPOSE 3001
CMD ["npm", "run", "dev"]
