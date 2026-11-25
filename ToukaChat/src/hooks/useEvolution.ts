"use client";
import { useState } from "react";
import { getActiveProxyUrl, fetchTouka } from "@/lib/connections";
import { logSSOTError, errorMsg, getResponseErrorCode, ToukaApiError } from "@/lib/error_handler";

export function useEvolution() {
    const [isInstalling, setIsInstalling] = useState(false);

    /** 核准安裝；成功回 true，失敗一律丟帶錯誤碼的 ToukaApiError，視窗才能說出原因。 */
    const confirmInstall = async (installRequestId: string, envVars: Record<string, string>) => {
        setIsInstalling(true);
        try {
            const proxyUrl = getActiveProxyUrl();
            let res: Response;
            try {
                // ADR-0148 Bug B：後端 /terminal/approve 以 TerminalApprovalRequest 解析，
                // 只有 `payload` 欄位——舊送 { env_vars } 會被 pydantic 丟棄 → env 為空 →
                // MCP server 無金鑰啟動 → 安裝回 Success 但 runtime 失敗。須包成 { payload }。
                res = await fetchTouka(proxyUrl, `/terminal/approve/${installRequestId}`, {
                    method: "POST",
                    body: JSON.stringify({ payload: { env_vars: envVars } })
                });
            } catch (cause) {
                // 斷線或逾時：瀏覽器的原文是英文，換成在地化的連線錯誤；原文留在 reason 給日誌。
                throw new ToukaApiError(errorMsg("NETWORK_CONNECTION_ERROR"), 0, "NETWORK_CONNECTION_ERROR", String(cause));
            }

            if (!res.ok) {
                // 不是管理員（403）、申請過期（404）等，後端會給錯誤碼；讀不到時才當成存檔失敗。
                const code = (await getResponseErrorCode(res)) ?? "CONFIG_SAVE_FAILED";
                throw new ToukaApiError(errorMsg(code), res.status, code);
            }
            return true;
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
