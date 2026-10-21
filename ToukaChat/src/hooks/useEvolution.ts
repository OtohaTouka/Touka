"use client";
import { useState } from "react";
import { getActiveProxyUrl, fetchTouka } from "@/lib/connections";
import { logSSOTError, errorMsg } from "@/lib/error_handler";

export function useEvolution() {
    const [isInstalling, setIsInstalling] = useState(false);

    const confirmInstall = async (installRequestId: string, envVars: Record<string, string>) => {
        setIsInstalling(true);
        try {
            const proxyUrl = getActiveProxyUrl();
            // ADR-0148 Bug B：後端 /terminal/approve 以 TerminalApprovalRequest 解析，
            // 只有 `payload` 欄位——舊送 { env_vars } 會被 pydantic 丟棄 → env 為空 →
            // MCP server 無金鑰啟動 → 安裝回 Success 但 runtime 失敗。須包成 { payload }。
            const res = await fetchTouka(proxyUrl, `/terminal/approve/${installRequestId}`, {
                method: "POST",
                body: JSON.stringify({ payload: { env_vars: envVars } })
            });

            if (res.ok) {
                return true;
            } else {
                throw new Error(errorMsg("CONFIG_SAVE_FAILED"));
            }
        } catch (err) {
            logSSOTError(err, "useEvolution:confirmInstall");
            throw err;
        } finally {
            setIsInstalling(false);
        }
    };

    return {
        isInstalling,
        confirmInstall
    };
}
