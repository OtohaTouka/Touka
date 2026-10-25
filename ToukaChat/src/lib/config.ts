/**
 * ToukaChat 全域配置 (Frontend SSOT)
 * 
 * 所有環境變數 (process.env) 必須透過此處封裝，嚴禁在組件中直接調用。
 */

export const CONFIG = {
    // 後端 Proxy 地址 (優先使用環境變數，否則預設為 localhost)
    PROXY_URL: (process.env.NEXT_PUBLIC_PROXY_URL || "").replace(/\/v1\/?$/, "").replace(/\/$/, ""),
    
    // 智慧節律器 (Heartbeat) 地址
    // 本機開發預設 localhost:8082；雲端部署請設定 NEXT_PUBLIC_HEARTBEAT_URL
    HEARTBEAT_URL: (process.env.NEXT_PUBLIC_HEARTBEAT_URL || "http://localhost:8082").replace(/\/v1\/?$/, "").replace(/\/$/, ""),

    // 其他配置項可以在此擴充
    DEFAULT_AUTH_LEVEL: "Base",
    
    IS_DEV: process.env.NODE_ENV === 'development',

    // 版本號：建置時由 next.config 的 env 從 package.json 注入
    APP_VERSION: process.env.NEXT_PUBLIC_APP_VERSION || "",
};
