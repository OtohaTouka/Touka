"use client";
import { useState, useCallback } from 'react';
import { getActiveProxyUrl, fetchTouka } from '@/lib/connections';
import { getSystemEpoch } from '@/lib/time_utils';
import { logSSOTError } from '@/lib/error_handler';
import type { Project } from '@/lib/types';

// O-fix: Project is the SSOT type in lib/types; re-exported here so existing
// `import { Project } from '@/hooks/useProjects'` consumers keep working without
// a second, drift-prone declaration.
export type { Project };

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

    const saveProject = async (project: Omit<Project, 'updatedAt'>) => {
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
            // root_path 422、權限 403）仍回 true，使 UI 佯裝儲存成功（誤導使用者）。
            if (!res.ok) {
                throw new Error(`projects/save failed: HTTP ${res.status}`);
            }
            await fetchProjects();
            return true;
        } catch (error) {
            logSSOTError(error, "useProjects:saveProject");
            return false;
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
        fetchProjectOverview,
        fetchProjectFiles,
        fetchWorkspaceRoot,
        saveWorkspaceRoot
    };
}
