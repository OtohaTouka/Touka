import type { components } from "@/types/api";

/** /v1/health 的回應形狀：系統狀態與換網址前的連得上檢查共用一份判斷。 */
export function validHealth(value: unknown): value is components["schemas"]["HealthResponse"] {
    if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
    const health = value as Record<string, unknown>;
    return (health.status === "healthy" || health.status === "degraded") &&
        typeof health.timestamp === "string" && typeof health.permission_config_degraded === "boolean" &&
        typeof health.permission_config_degraded_reason === "string";
}
