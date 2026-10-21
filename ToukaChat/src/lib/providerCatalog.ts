/**
 * 供應商目錄（Provider Catalog）—— 連線設定的單一事實來源（SSOT）。
 *
 * 供應商清單、預設 base_url、文件連結、i18n key 只定義在這裡一次；兩個連線 UI
 * （設定頁 AddConnectionForm 與獨立 /connections 頁）與 connections.ts 的 BackendProvider
 * 型別都從這份目錄衍生，杜絕「兩份不同步」的漂移（例如 LiteLLM 曾只出現在其中一個 UI）。
 *
 * docUrl 一律寫死於此（不從設定/API 動態載入），避免說明連結被竄改為釣魚網址。
 */
import type { BackendProtocol } from './types';

export interface ProviderMeta {
  /** 顯示用 emoji（非語言相關，故放目錄而非 i18n）。 */
  icon: string;
  /** 選到此供應商時預填的 base_url（官方文件標準值；經 buildChatEndpoint 組裝皆正確）。 */
  defaultBaseUrl: string;
  /** 設定文件連結（寫死＝防釣魚）。 */
  docUrl: string;
  /** i18n key 前綴：`<i18nBase>.label`（選項文字）、`<i18nBase>.hint`（設定說明）。 */
  i18nBase: string;
}

/** 供應商目錄 SSOT。key 即 provider id。 */
export const PROVIDER_CATALOG = {
  proxy: {
    icon: '🛡️',
    defaultBaseUrl: 'http://localhost:8000',
    docUrl: 'https://github.com/anthropics/claude-code',
    i18nBase: 'connections.providers.proxy',
  },
  openai: {
    icon: '🌌',
    defaultBaseUrl: 'https://api.openai.com/v1',
    docUrl: 'https://platform.openai.com/docs/api-reference/chat',
    i18nBase: 'connections.providers.openai',
  },
  ollama: {
    icon: '🦙',
    defaultBaseUrl: 'http://localhost:11434',
    docUrl: 'https://github.com/ollama/ollama/blob/main/docs/api.md',
    i18nBase: 'connections.providers.ollama',
  },
  openrouter: {
    icon: '🚀',
    defaultBaseUrl: 'https://openrouter.ai/api/v1',
    docUrl: 'https://openrouter.ai/docs/quickstart',
    i18nBase: 'connections.providers.openrouter',
  },
  litellm: {
    icon: '🚂',
    defaultBaseUrl: 'http://localhost:4000',
    docUrl: 'https://docs.litellm.ai/docs/simple_proxy',
    i18nBase: 'connections.providers.litellm',
  },
} as const satisfies Record<string, ProviderMeta>;

/** 供應商 id 型別（由目錄 keys 衍生，SSOT）。 */
export type ProviderKey = keyof typeof PROVIDER_CATALOG;

/** 供應商 id 清單（給 UI 渲染選項用）。 */
export const PROVIDER_IDS = Object.keys(PROVIDER_CATALOG) as ProviderKey[];

/** 所有供應商預設 base_url 集合（內部用：判斷使用者是否仍是預設值；見 resolvePrefillUrl）。 */
const DEFAULT_BASE_URLS = new Set<string>(
  PROVIDER_IDS.map((id) => PROVIDER_CATALOG[id].defaultBaseUrl),
);

/**
 * 智慧預填：選到供應商時，回傳 URL 欄該填入的值（純函式，兩個連線 UI 共用）。
 *
 * 規則：欄位為空、或仍是「某供應商的預設值」→ 換成新供應商的預設值；否則視為使用者自訂、
 * 原樣保留（不覆寫）。讓切換供應商時自動帶出正確 base_url，又不破壞使用者手動輸入。
 */
export function resolvePrefillUrl(provider: string, prevUrl: string): string {
  const def = PROVIDER_CATALOG[provider as ProviderKey]?.defaultBaseUrl ?? '';
  return !prevUrl || DEFAULT_BASE_URLS.has(prevUrl) ? def : prevUrl;
}

const OPENAI_CHAT_PATH = '/v1/chat/completions';

/** 決定某供應商（與協定）對應的 chat 端點路徑後綴。 */
function chatPathFor(provider: string, protocol?: BackendProtocol | string): string {
  if (provider === 'ollama') return '/api/chat';
  if (provider === 'proxy' && protocol === 'anthropic') return '/v1/messages';
  return OPENAI_CHAT_PATH;
}

/**
 * 由使用者填入的 base_url 組出正確的 chat 端點 URL（SSOT；解雙 /v1 拼接）。
 *
 * 規則：去尾斜線 → 若 base 已是完整端點則原樣 → 若 base 以 `/v1` 結尾且目標路徑也在 `/v1`
 * 之下則只補後半段（避免 `/v1/v1/...`）→ 否則直接補完整後綴。讓使用者填 base、含 /v1 的
 * base、或完整端點網址三種都正確。
 */
export function buildChatEndpoint(
  provider: string,
  baseUrl: string,
  protocol?: BackendProtocol | string,
): string {
  const path = chatPathFor(provider, protocol);
  const base = (baseUrl || '').trim().replace(/\/+$/, '');
  if (base.toLowerCase().endsWith(path.toLowerCase())) return base;
  if (/\/v1$/i.test(base) && path.startsWith('/v1')) {
    return base + path.slice('/v1'.length);
  }
  return base + path;
}
