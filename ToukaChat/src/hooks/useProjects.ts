"use client";
import { useState, useCallback } from 'react';
import { getActiveProxyUrl, fetchTouka } from '@/lib/connections';
import { getSystemEpoch, getSystemNow } from '@/lib/time_utils';
import { getResponseDetailString, getResponseErrorCode, logSSOTError } from '@/lib/error_handler';
import type { ProjectSaveFailure } from '@/lib/project_save_wording';
import { projectDownloadFileName, type ProjectDownloadFailure } from '@/lib/project_download';
import type { Project } from '@/lib/types';

// O-fix: Project is the SSOT type in lib/types; re-exported here so existing
// `import { Project } from '@/hooks/useProjects'` consumers keep working without
// a second, drift-prone declaration.
export type { Project };

/** 存專案的結果：成功，或被拒絕（帶伺服器給的錯誤碼與原因；斷線時兩個都沒有）。 */
export type ProjectSaveResult = { ok: true } | ({ ok: false } & ProjectSaveFailure);

/** 下載的 zip 交給瀏覽器後，blob 網址留多久才放掉。 */
const DOWNLOAD_URL_LIFETIME_MS = 40_000;

/** 下載專案的結果：成功（已交給瀏覽器存檔），或被拒絕（帶錯誤碼與原因；斷線時兩個都沒有）。 */
export type ProjectDownloadResult = { ok: true } | ({ ok: false } & ProjectDownloadFailure);

/** 被拒絕的回應帶回錯誤碼與原因，畫面才說得出為什麼。 */
async function responseFailure(res: Response) {
    return {
        code: await getResponseErrorCode(res),
        reason: await getResponseDetailString(res, "reason"),
    };
}

export interface FileNode {
    name: string;
    type: 'file' | 'directory';
    path: string;
    size?: number;
    children?: FileNode[];
}

export interface ProjectOverview {
    project: {
        id: string;
        name: string;
        root_path: string;
        description: string;
    };
    recent_audits: Array<{
        action: string;
        summary: string;
        timestamp: number;
        agent_id: string;
    }>;
    sessions: Array<{
        id: string;
        title: string;
        updatedAt: number;
        agent_id: string;
    }>;
    agents: string[];
}

export function useProjects() {
    const [projects, setProjects] = useState<Project[]>([]);
    const [isLoading, setIsLoading] = useState(true);

    const fetchProjects = useCallback(async () => {
        setIsLoading(true);
        try {
            const proxyUrl = getActiveProxyUrl();
            const res = await fetchTouka(proxyUrl, "/v1/ui/projects/list");
            // fetchTouka 不會對非 2xx 丟例外；任一錯誤狀態的 body 是合法 JSON 錯誤物件
            // ({detail:{...}})，若直接灌入 setProjects 會讓 projects 變非陣列、render 期崩潰。
            if (!res.ok) {
                throw new Error(`projects/list failed: HTTP ${res.status}`);
            }
            const data = await res.json();
            // SSOT 不變式：projects 永遠是陣列；非陣列 body 強制收斂為空陣列。
            setProjects(Array.isArray(data) ? data : []);
        } catch (error) {
            // 失敗時不觸碰 projects（保留前次良好陣列），僅記錄；不變式恆成立。
            logSSOTError(error, "useProjects:fetchProjects");
        } finally {
            setIsLoading(false);
        }
    }, []);

    const saveProject = async (project: Omit<Project, 'updatedAt'>): Promise<ProjectSaveResult> => {
        try {
            const proxyUrl = getActiveProxyUrl();
            const payload = {
                ...project,
                updatedAt: getSystemEpoch()
            };

            const res = await fetchTouka(proxyUrl, "/v1/ui/projects/save", {
                method: 'POST',
                body: JSON.stringify(payload)
            });
            // fetchTouka 不對非 2xx 丟例外；須顯式檢查 res.ok，否則被後端拒絕（如越界
            // root_path 422、權限 403）仍回成功，使 UI 佯裝儲存成功（誤導使用者）。
            // 被拒絕時帶回錯誤碼與原因（專用空間的登記規則，ADR-0332），畫面才說得出為什麼。
            if (!res.ok) {
                const failure = await responseFailure(res);
                logSSOTError(new Error(`projects/save failed: HTTP ${res.status}`), "useProjects:saveProject");
                return { ok: false, ...failure };
            }
            await fetchProjects();
            return { ok: true };
        } catch (error) {
            logSSOTError(error, "useProjects:saveProject");
            return { ok: false };
        }
    };

    const deleteProject = async (id: string) => {
        try {
            const proxyUrl = getActiveProxyUrl();
            const res = await fetchTouka(proxyUrl, `/v1/ui/projects/${id}`, { method: 'DELETE' });
            if (!res.ok) {
                throw new Error(`projects/delete failed: HTTP ${res.status}`);
            }
            await fetchProjects();
            return true;
        } catch (error) {
            logSSOTError(error, "useProjects:deleteProject");
            return false;
        }
    };

    // 下載專用空間的專案（ADR-0332 PR B）：經 fetchTouka 帶登入拿 zip，交給瀏覽器存檔；
    // 不做帶 token 的下載連結。
    const downloadProject = async (project: Project): Promise<ProjectDownloadResult> => {
        try {
            const proxyUrl = getActiveProxyUrl();
            const path = `/v1/ui/projects/${encodeURIComponent(project.id)}/download`;
            const res = await fetchTouka(proxyUrl, path);
            if (!res.ok) {
                const failure = await responseFailure(res);
                logSSOTError(new Error(`projects/download failed: HTTP ${res.status}`), "useProjects:downloadProject");
                return { ok: false, ...failure };
            }
            const url = URL.createObjectURL(await res.blob());
            const link = document.createElement("a");
            link.href = url;
            link.download = projectDownloadFileName(project.name, getSystemNow());
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
            // 有些瀏覽器在點下去之後才開始讀 blob：等一下再放掉（和 FileSaver.js 相同的 40 秒）
            setTimeout(() => URL.revokeObjectURL(url), DOWNLOAD_URL_LIFETIME_MS);
            return { ok: true };
        } catch (error) {
            logSSOTError(error, "useProjects:downloadProject");
            return { ok: false };
        }
    };

    const fetchProjectOverview = useCallback(async (projectId: string) => {
        try {
            const proxyUrl = getActiveProxyUrl();
            const res = await fetchTouka(proxyUrl, `/v1/ui/projects/${projectId}/overview`);
            return await res.json();
        } catch (error) {
            logSSOTError(error, "useProjects:fetchProjectOverview");
            return null;
        }
    }, []);

    const fetchProjectFiles = useCallback(async (projectId: string) => {
        try {
            const proxyUrl = getActiveProxyUrl();
            const res = await fetchTouka(proxyUrl, `/v1/ui/projects/${projectId}/files`);
            const data = await res.json();
            return data.tree || [];
        } catch (error) {
            logSSOTError(error, "useProjects:fetchProjectFiles");
            return [];
        }
    }, []);

    const fetchWorkspaceRoot = useCallback(async () => {
        try {
            const proxyUrl = getActiveProxyUrl();
            const res = await fetchTouka(proxyUrl, "/v1/ui/settings/workspace");
            if (res.ok) {
                const data = await res.json();
                return data.workspace_root;
            }
            return "";
        } catch (error) {
            logSSOTError(error, "useProjects:fetchWorkspaceRoot");
            return "";
        }
    }, []);

    const saveWorkspaceRoot = async (path: string) => {
        try {
            const proxyUrl = getActiveProxyUrl();
            const res = await fetchTouka(proxyUrl, "/v1/ui/settings/workspace", {
                method: 'POST',
                body: JSON.stringify({ workspace_root: path })
            });
            return res.ok;
        } catch (error) {
            logSSOTError(error, "useProjects:saveWorkspaceRoot");
            return false;
        }
    };

    return {
        projects,
        isLoading,
        fetchProjects,
        saveProject,
        deleteProject,
        downloadProject,
        fetchProjectOverview,
        fetchProjectFiles,
        fetchWorkspaceRoot,
        saveWorkspaceRoot
    };
}
