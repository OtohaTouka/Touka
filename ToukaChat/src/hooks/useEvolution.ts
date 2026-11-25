"use client";
import { useState } from "react";
import { getActiveProxyUrl, fetchTouka } from "@/lib/connections";
import { logSSOTError, errorMsg, getResponseErrorCode, ToukaApiError } from "@/lib/error_handler";

export function useEvolution() {
    const [isInstalling, setIsInstalling] = useState(false);

    /** 核准安裝；成功回 true。HTTP 失敗與斷線丟帶錯誤碼的 ToukaApiError（視窗據此說出原因），其他意外錯誤原樣丟出。 */
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
                // 斷線或逾時：瀏覽器的原文是英文，只記進日誌；畫面換成在地化的連線錯誤。
                logSSOTError(cause, "useEvolution:confirmInstall:network");
                throw new ToukaApiError(errorMsg("NETWORK_CONNECTION_ERROR"), 0, "NETWORK_CONNECTION_ERROR");
            }

            if (!res.ok) {
                // 不是管理員（403）等，後端會給錯誤碼。/terminal 只掛根路徑（main.py 的 APP_ROUTERS），
                // fetchTouka 遇 404 會改打 /v1，拿回的是那次的通用 404，原本的錯誤碼看不到；
                // 這個端點的 404 只會是申請已經不在（過期或已處理）。其他讀不到錯誤碼的當成存檔失敗。
                const fallback = res.status === 404 ? "TERMINAL_APPROVAL_NOT_FOUND" : "CONFIG_SAVE_FAILED";
                const code = (await getResponseErrorCode(res)) ?? fallback;
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
