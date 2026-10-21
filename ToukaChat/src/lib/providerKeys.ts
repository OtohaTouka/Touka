/**
 * 系統級 provider API 金鑰清單 —— 單一事實來源（SSOT）。
 *
 * 這些是存在後端 .env 的「系統級」金鑰（與「外部連線」不同）。初次精靈 Step 3（唯讀狀態）
 * 與設定頁的「API 金鑰」分頁共用這份清單，避免兩份不同步。
 */
interface ProviderKeyMeta {
  id: string;
  label: string;
  env: string;
  /** 各供應商「金鑰控制台」連結。寫死＝防釣魚（絕不從 API/設定載入，對齊 providerCatalog.ts docUrl）。 */
  helpUrl?: string;
}

export const PROVIDER_KEYS: ProviderKeyMeta[] = [
  { id: "anthropic", label: "Anthropic (Claude)", env: "ANTHROPIC_API_KEY", helpUrl: "https://console.anthropic.com/settings/keys" },
  { id: "openai", label: "OpenAI", env: "OPENAI_API_KEY", helpUrl: "https://platform.openai.com/api-keys" },
  { id: "deepseek", label: "DeepSeek", env: "DEEPSEEK_API_KEY", helpUrl: "https://platform.deepseek.com/api_keys" },
  { id: "gemini", label: "Google Gemini", env: "GEMINI_API_KEY", helpUrl: "https://aistudio.google.com/apikey" },
  { id: "grok", label: "xAI Grok", env: "GROK_API_KEY", helpUrl: "https://console.x.ai/team/default/api-keys" },
  { id: "tavily", label: "Tavily Search", env: "TAVILY_API_KEY", helpUrl: "https://app.tavily.com/home" },
];

/**
 * 後端 `GET /config/env` 對 secret 類值回傳「遮罩佔位」（非空）或空字串（未設定）。
 * 非空即代表「已設定」——前端永遠拿不到真值，只看得到 set/unset。
 */
export function isKeySet(maskedValue: string | undefined): boolean {
  return !!maskedValue;
}
