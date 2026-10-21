/**
 * MCP Hub 伺服器的前端視圖型別。
 *
 * 後端 codegen `MCPHubServerItem` schema 未涵蓋 `icon`/`source_url`
 * 等實際存在的欄位，故前端以此 view 型別反映真實消費形狀（避免散落 `any`）。
 */
export interface McpHubServerView {
  id: string;
  name?: string | null;
  description?: string | null;
  category?: string | null;
  /** stdio 條目才有；remote 條目為空（ADR-0192，兩者互斥）。 */
  command?: string | null;
  args?: string[] | null;
  icon?: string | null;
  source_url?: string | null;
  downloads?: number | null;
  /** remote 條目（ADR-0192）：streamable HTTP 端點；與 command/args 互斥。 */
  url?: string | null;
  headers?: Record<string, string> | null;
  env_requirements?: string[] | null;
  popularity?: number | null;
  npm_package?: string | null;
  /** 徽章比對鍵＝後端 derive_server_key(name) 投影（ADR-0190）；缺席時後備用 id。 */
  server_key?: string | null;
}

/** MCP Hub 伺服器相對於已裝設定的版本狀態（與 skill 版本追蹤對等）。 */
export type McpStatus = "not_installed" | "up_to_date" | "update_available";

/**
 * 將 npm 下載量格式化為 Hub 卡片顯示字串（誠實顯示）。
 *
 * - 無數據（null/undefined：npm 抓取失敗或非 npm 條目）→「精選」，不偽造數字
 * - 真 0 → "0"（誠實呈現 API 回傳的零）
 * - 其餘 → 千分位縮寫（如 1234 → "1.2k"）
 */
export function formatHubDownloads(
  downloads: number | null | undefined,
): string {
  if (downloads == null) return "精選";
  if (downloads === 0) return "0";
  return (downloads / 1000).toFixed(1) + "k";
}

/**
 * 已安裝 MCP 設定（`server_config.json` 的 `mcpServers`）。
 *
 * key＝後端 install 落盤鍵 `derive_server_key(name)`（ADR-0102，如 `fetch`），
 * **不是** hub.id（帶 `curated-`/`official-` 前綴）；手動新增條目的 key 則為使用者自取名。
 */
export type InstalledMcpConfig = Record<
  string,
  {
    command?: string;
    args?: string[];
    url?: string;
    headers?: Record<string, string>;
    env?: Record<string, string>;
    enabled?: boolean;
    /**
     * per-installation 工具治理（ADR-0195 軸 A）。`undefined`/`null` ＝未設＝全開；
     * `["*"]` ＝明示全開（「全部開啟」必須送這個，送 `null` 會被後端自磁碟還原成舊值）；
     * `[]` ＝明示全關。此政策對**所有身分**生效，Owner 也不例外。
     */
    enabled_tools?: string[] | null;
    /** 硬阻擋清單，恆優先於 `enabled_tools`。UI 不驅動——手寫設定的逃生口。 */
    disabled_tools?: string[];
    /** P6 SDK cache/listener 可獨立關閉；關閉不影響 MCP 主連線。 */
    cache_enabled?: boolean;
    cache_max_entries?: number;
    cache_default_ttl_ms?: number;
    subscriptions_enabled?: boolean;
    subscription_max_backoff_seconds?: number;
    /** 敏感 x-mcp-header 的 owner 逐工具核准清單。 */
    approved_header_tools?: string[];
    icons_enabled?: boolean;
    telemetry_enabled?: boolean;
    auth_mode?:
      | "none"
      | "static_headers"
      | "oauth_user"
      | "oauth_client_credentials"
      | "enterprise";
    auth_profile_id?: string | null;
    workload_authority_kind?: "direct" | "owner_delegation" | null;
    workload_delegation_id?: string | null;
    workload_source_installation_id?: string | null;
  }
>;

/** MCP P5 API 視圖型別全部引用 OpenAPI codegen；禁止手刻第二份合約。 */
export type McpServerToolsView =
  components["schemas"]["MCPServerToolsResponse"];
export type McpServerResourcesView =
  components["schemas"]["MCPServerResourcesResponse"];
export type McpServerResourceTemplatesView =
  components["schemas"]["MCPServerResourceTemplatesResponse"];
export type McpResourceReadView =
  components["schemas"]["MCPResourceReadResponse"];
export type McpServerPromptsView =
  components["schemas"]["MCPServerPromptsResponse"];
export type McpPromptGetView = components["schemas"]["MCPPromptGetResponse"];
export type McpOAuthStatus = components["schemas"]["McpOAuthStatus"];
export type McpOAuthStatusView =
  components["schemas"]["McpOAuthStatusResponse"];
export type McpOAuthStartView = components["schemas"]["McpOAuthStartResponse"];
export type McpOAuthDisconnectView =
  components["schemas"]["McpOAuthDisconnectResponse"];
export type WorkloadOAuthProfileView =
  components["schemas"]["WorkloadOAuthProfile"];
export type WorkloadCredentialStatusView =
  components["schemas"]["WorkloadCredentialStatus"];
export type WorkloadDelegationView =
  components["schemas"]["WorkloadDelegationView"];
export type WorkloadDelegationInput =
  components["schemas"]["WorkloadDelegationPutRequest"];
export type WorkloadMutationView =
  components["schemas"]["WorkloadMutationResponse"];
export type WorkloadMetadataView =
  components["schemas"]["WorkloadMetadataStatus"];
export type WorkloadRecoveryView =
  components["schemas"]["WorkloadRecoveryItem"];
export type WorkloadKeyRotationView =
  components["schemas"]["OAuthKeyRotationRecord"];

/**
 * 哨兵引數位（如 `@TOUKA_APPDATA@`）——SSOT 在後端 `mcp_sentinels.py`；install 落盤時
 * 已解析為各機絕對路徑（ADR-0186），前端無從得知解析值，僅能以「哨兵位視為通配」比對。
 */
const SENTINEL_ARG_RE = /^@TOUKA_[A-Z0-9_]+@$/;

/** 逐元素比對 args：長度須相同；哨兵位接受任何已解析值，其餘位須全等。 */
function argsMatchRecipe(hubArgs: string[], installedArgs: string[]): boolean {
  if (hubArgs.length !== installedArgs.length) return false;
  return hubArgs.every(
    (a, i) => a === installedArgs[i] || SENTINEL_ARG_RE.test(a),
  );
}

/**
 * Hub 條目在 `server_config.json` 裡的鍵（SSOT）。
 *
 * 後端 install 落盤鍵＝`derive_server_key(name)`，由 `/hub` 端點投影為 `server_key`
 * （ADR-0190）；`id` 僅作無 `server_key` 舊 payload 的後備。**安裝寫入與徽章查表必須
 * 共用此函式**——ADR-0190 只修了查表側，寫入側仍用 `hub.id`，於是同一服務會落成兩個
 * 不同的鍵：徽章永遠不亮，且「更新時保留既有 env 密鑰」因取不到舊條目而失效
 * （ADR-0192 補齊）。
 */
export function hubEntryConfigKey(hubSrv: McpHubServerView): string {
  return hubSrv.server_key ?? hubSrv.id;
}

/**
 * 依 Hub 條目與使用者填入的憑證，組出要寫進 `server_config.json` 的條目。
 *
 * stdio 帶 command/args；remote（ADR-0192）帶 url/headers——headers 是 `${VAR}` 模板，
 * 憑證實值一律落在 env。既有 env 以新輸入覆蓋同名 key（更新時不清掉未重填的密鑰）。
 *
 * 工具治理政策（ADR-0195 軸 A）亦自 `existing` 帶回：本函式重建整個條目，未列名的欄位
 * 一律消失，而 Hub「更新設定」會走這條路——不帶回等於按一次更新就把 `create_refund`
 * 重新打開。前端 Hub 安裝**不打 `/hub/install`**（SettingsView 客戶端組裝後走
 * `/ui/config`），故後端 `install()` 的政策傳遞在此路徑上不生效，只能在這裡補。
 */
export function buildInstalledEntry(
  hubSrv: McpHubServerView,
  envVars: Record<string, string>,
  existing: InstalledMcpConfig[string] | undefined,
): InstalledMcpConfig[string] {
  const mergedEnv = { ...(existing?.env ?? {}), ...envVars };
  const env = Object.keys(mergedEnv).length > 0 ? mergedEnv : undefined;
  const policy = {
    enabled_tools: existing?.enabled_tools,
    disabled_tools: existing?.disabled_tools,
    cache_enabled: existing?.cache_enabled,
    cache_max_entries: existing?.cache_max_entries,
    cache_default_ttl_ms: existing?.cache_default_ttl_ms,
    subscriptions_enabled: existing?.subscriptions_enabled,
    subscription_max_backoff_seconds:
      existing?.subscription_max_backoff_seconds,
    approved_header_tools: existing?.approved_header_tools,
    icons_enabled: existing?.icons_enabled,
    telemetry_enabled: existing?.telemetry_enabled,
  };
  return hubSrv.url
    ? {
        url: hubSrv.url,
        headers: hubSrv.headers ?? {},
        env,
        enabled: true,
        ...policy,
      }
    : {
        command: hubSrv.command ?? undefined,
        args: hubSrv.args ?? [],
        env,
        enabled: true,
        ...policy,
      };
}

/**
 * 比對 Hub 伺服器與已裝設定，判定版本狀態（recipe-drift）。
 *
 * - `not_installed`：`mcpConfig` 無此比對鍵（`server_key`，缺席時後備 `id`）。
 * - `up_to_date`：已裝且 `command` ＋ `args` 與 hub 建議一致（哨兵位通配）。
 * - `update_available`：已裝但 hub 建議的 `command`/`args` 已變（recipe 漂移）。
 *
 * 註：ADR-0191 起 hub 條目一律釘版（npx `pkg@x.y.z`／uvx `pkg==x.y.z`），故套件名位
 * 本身帶版本 —— 策展人升版即構成 recipe 漂移，使用者這才第一次真的看得到「可更新」。
 * （釘版前 MCP 多以浮動 `npx/uvx` 最新版執行，此偵測只在「建議指令設定變動」時觸發、
 * 對「套件有新版本」恆無訊號。）
 *
 * ⚠️ 工具治理欄位（ADR-0195）**刻意不納入**此比對：它們是使用者的本地選擇、不是策展配方
 * 的一部分，納入會讓「勾掉一個工具」顯示成「可更新」。
 */
export function computeMcpStatus(
  hubSrv: McpHubServerView,
  mcpConfig: InstalledMcpConfig,
): McpStatus {
  const installed = mcpConfig[hubEntryConfigKey(hubSrv)];
  if (!installed) return "not_installed";
  // remote 條目（ADR-0192）無本地指令，改以端點比對：策展改了 URL＝可更新。
  const sameUrl = (hubSrv.url ?? null) === (installed.url ?? null);
  const sameCommand = installed.command === hubSrv.command;
  const sameArgs = argsMatchRecipe(hubSrv.args ?? [], installed.args ?? []);
  return sameUrl && sameCommand && sameArgs ? "up_to_date" : "update_available";
}
import type { components } from "@/types/api";
