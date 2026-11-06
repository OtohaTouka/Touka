import type { components, operations } from '@/types/api';

export type HeartbeatSchedule = components['schemas']['HeartbeatSchedule'];
export type SaveResult = components['schemas']['SaveResult'];
export type SessionSaveResponse = components['schemas']['SessionSaveResponse'];
export type StreamStartedEvent = components['schemas']['StreamStartedEvent'];
export type StreamTerminalEvent = components['schemas']['StreamTerminalEvent'];
export type UiPersistence = components['schemas']['UiPersistence'];
export type SessionSnapshot = components['schemas']['PresentSessionSnapshot'] | components['schemas']['AbsentSessionSnapshot'];
export type BufferedStreamState = components['schemas']['BufferedStreamState'];
export type Interjection = components['schemas']['Interjection'];
export type RestoreStatus = components['schemas']['RestoreStatus'];
export type AgentResetRequest = components['schemas']['AgentResetRequest'];
export type AgentResetResponse = components['schemas']['AgentResetResponse'];
export type LastResetStatus = components['schemas']['LastResetStatus'];
export type MemoryScrubReport = components['schemas']['MemoryScrubReport'];
export type ScrubItemResult = components['schemas']['ScrubItemResult'];

// ADR-0292：Hook 設定頁
export type HooksConfig = components['schemas']['HooksConfig-Input'];
export type HookMatcherGroup = components['schemas']['HookMatcherGroup'];
export type CommandHook = components['schemas']['CommandHook'];
export type HttpHook = components['schemas']['HttpHook'];
export type HookHandler = CommandHook | HttpHook;
export type HookHealth = components['schemas']['HookHealthOut'];
export type HookRun = components['schemas']['HookRunOut'];
export type HooksConfigResponse = components['schemas']['HooksConfigResponse'];

export type SoulUpdateInput = components['schemas']['SoulUpdateRequest'];
export type SoulUpdateResult = operations['update_soul_v1_souls__soul_id__post']['responses'][200]['content']['application/json'];

export type BackendProtocol = 'openai' | 'anthropic';

export interface VSCodeBridge {
    postMessage(message: Record<string, unknown>): void;
}

export interface Connection {
    id: string;
    name: string;
    url: string;
    apiKey: string;
    provider: string;
    protocol: string;
    enabled?: boolean;
    /** P5-#4：請求逾時（毫秒）；未設或 0＝不限。僅套用於一般請求（fetchTouka），不影響聊天串流。 */
    requestTimeoutMs?: number;
}

export interface Model {
    id: string;
    object: string;
    owned_by: string;
    name?: string;
    is_agent?: boolean;
}

export interface Tool {
    name: string;
    description: string;
    inputSchema?: Record<string, unknown>;
    category?: string;
}

export interface McpServerStatus {
    name: string;
    generation?: number;
    isolation?: Pick<McpIsolationView, 'class' | 'origin' | 'record_state'> & {
        state: 'running' | 'not_started' | 'waiting_isolation' | 'waiting_probe' | 'suspended';
        level: components['schemas']['IsolationStatusResponse']['level'];
        features: string[];
        reason: string | null;
        manual_start?: {
            fallback: NonNullable<McpRestartRequest['isolation_ack']>;
            features: string[];
            generation: number;
            bound: string;
        };
    } | null;
    status: 'connected' | 'error' | 'disconnected' | 'not_started' | 'circuit_open';
    tools_count: number;
    url: string;
    // 健康管理：最後脫敏錯誤、斷路器狀態、重啟次數（供 MCP 分頁狀態徽章/診斷顯示）。
    last_error?: string | null;
    last_error_at?: number | null;
    circuit_state?: 'CLOSED' | 'OPEN' | 'HALF_OPEN' | null;
    restart_count?: number;
    cache?: {
        enabled: boolean;
        entries: number;
        public_entries: number;
        private_entries: number;
        hits: number;
        misses: number;
        writes: number;
        evictions: number;
        max_entries: number;
    };
    subscription?: {
        enabled: boolean;
        status: 'disabled' | 'not_started' | 'listening' | 'stale';
        stale: boolean;
        reconnect_attempts: number;
        handle_count: number;
    };
}

export type McpHubServer = components["schemas"]["MCPHubServerItem"];
export type McpConfigSaveResponse = components["schemas"]["McpConfigSaveResponse"];
export type McpIsolationView = components["schemas"]["McpIsolationView"];
export type McpRestartRequest = components["schemas"]["McpRestartRequest"];
export type McpIsolationUpdateRequest = components["schemas"]["McpIsolationUpdateRequest"];
export type SandboxPathGrant = components["schemas"]["SandboxPathGrant"];
// The API returns the sanitised UserOut projection everywhere (admin endpoints,
// /auth/me, /auth/register) — never the internal User with password hash / refresh JTIs.
export type User = components["schemas"]["UserOut"];
export type UserCreate = components["schemas"]["UserCreate"];

export interface Skill {
    id: string;
    name: string;
    description: string;
    system_prompt: string;
    required_tools: string[];
    mcp_servers: string[];
    is_system?: boolean;
    // ADR-0074 版本追蹤：catalog 安裝來源 provenance（自建/URL 安裝為 null/缺）。
    source_catalog_id?: string | null;
    source_commit?: string | null;
}

export interface CustomModel {
    id: string;
    name: string;
    description: string;
    systemPrompt: string;
    baseModel: string;
    temperature: number;
    topP: number;
    topK: number;
    enabledTools: string[];
    safetyMode?: "off" | "balanced" | "strict";
    safetySettings?: Array<{ category: string; threshold: string }>;
}

export interface Session {
    /** 同 scope/server generation 的內容版本；不是授權憑證。 */
    revision?: string;
    id: string;
    title: string;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    messages: any; // 支援平鋪 [unknown[]] 或 樹狀 [TreeHistory] 格式
    updatedAt: number;
    is_home?: boolean;
    home_for_agent?: string | null;
    agent_id?: string | null;
    project_id?: string | null;
    auth_level?: string;
    active_mcp_servers?: string[];
    active_skills?: string[];
    round_count?: number;
    context_summary?: string | null;
    /** HAX G17（ADR-0086）：無痕對話——只存記憶體，不持久化、不匯出。 */
    incognito?: boolean;
    /** ADR-0087：深入研究模式——本 session 的對話走多步搜尋＋引用報告。 */
    deepResearch?: boolean;
    // ADR-0161：MCP 動態掛載模式旗標（逐則、記憶體內、切走歸零，同 deepResearch 語意）。
    mcpDispatcher?: boolean;
}

export interface Project {
    id: string;
    name: string;
    root_path: string;
    description: string;
    updatedAt: number;
}

export type AutonomyConfig = components["schemas"]["AutonomyConfigModel"];
export type ReviewerModelOption = components["schemas"]["ReviewerModelOption"];

export interface PermissionConfig {
    roles: Record<string, {
        desc?: string;
        allowed_tools: string[];
        denied_tools: string[];
        allowed_mcp_servers: string[];
        allowed_skills: string[];
        /**
         * MCP 工具面拒絕清單（ADR-0195 軸 B），形如 `server/tool` 或 `server/*`。
         * 選填：既有 permissions.json 無此欄位，設為必填會讓載入的舊設定型別不合。
         */
        denied_mcp_tools?: string[];
    }>;
    agent_mappings: Record<string, string>;
    mcp_servers?: string[];
    system_config: {
        default_role: string;
        allow_unregistered_agents: boolean;
    };
}

export interface IdentityMap {
    tag_rules: Record<string, {
        role: string;
        capabilities: string[];
    }>;
    agent_overrides: Record<string, {
        role: string;
        capabilities: string[];
    }>;
    default_role: string;
}

export interface AgentIdentity {
    id: string;
    name: string;
    type: string;
    is_group: boolean;
    aliases?: string[];
    // tag_rules-aware 有效角色／能力（後端 get_all_agents 附；ADR-0158 後續）：
    // 無 override 的 agent 顯示這裡的真實角色（是靈魂即 Owner），不再一律 default_role。
    effective_role?: string;
    effective_capabilities?: string[];
    has_override?: boolean;
}


export interface AskUserQuestion {
    question: string;
    options: string[];
    multiSelect?: boolean;
    allowOther?: boolean;
    header?: string;
}

/** HAX G10（ADR-0085）：ask_user 工具的提問請求（經 SSE ask_user_request 事件抵達前端）。 */
export interface AskUserRequest {
    id: string; // ask_user token
    questions: AskUserQuestion[];
}

export interface SkillInstallRequest {
    id: string; // 審核 ID (exec_id)
    skill_id: string;
    name: string;
    description: string;
    command: string;
    // 演化部署（skill_creation_approval_request，is_creation=true）僅帶 source_code，
    // 不含 args／permissions／env_requirements（launch command 於核准後才組出）——故三者
    // 對安裝事件必填、對部署事件為選填。
    args?: string[];
    permissions?: string[];
    env_requirements?: string[];
    source_code?: string;
    is_creation?: boolean;
}

// ADR-0074：策展真技能的單一檔案（相對路徑 ＋ 釘 sha256，安裝時逐檔內容驗證）。
export interface CatalogFile {
    path: string;
    sha256: string;
}

// ADR-0074：真技能策展目錄一筆（能力型 .skill，含可執行腳本；釘 commit 從 raw 抓取安裝）。
export interface CatalogSkill {
    id: string;
    name: string;
    category: string;
    description: string;
    repo: string;
    commit: string;
    base_path: string;
    files: CatalogFile[];
    author: string;
    license: string;
}
