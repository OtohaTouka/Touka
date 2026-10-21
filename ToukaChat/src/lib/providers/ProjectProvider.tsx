"use client";

import React, { createContext, useContext, useState, useCallback, useEffect } from "react";
import { Project } from "../types";
import { getActiveProxyUrl, fetchTouka } from "../connections";
import { logSSOTError } from "@/lib/error_handler";

interface ProjectContextType {
  projects: Project[];
  refreshProjects: () => Promise<void>;
}

const ProjectContext = createContext<ProjectContextType | undefined>(undefined);

export function ProjectProvider({ children }: { children: React.ReactNode }) {
  const [projects, setProjects] = useState<Project[]>([]);

  const refreshProjects = useCallback(async () => {
    try {
      const proxyUrl = getActiveProxyUrl();
      const res = await fetchTouka(proxyUrl, '/v1/ui/projects/list');
      if (res.ok) {
        const data = await res.json();
        setProjects(data);
      }
    } catch (e) {
      logSSOTError(e, "ProjectProvider:refreshProjects");
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- 掛載時載入專案清單（非同步 setState 屬預期）
    refreshProjects();
    // ADR-0153（同族自癒）：認證狀態變動後重探。修「/login 無卡首探 401 → 專案分組
    // 退化『未知專案』→ 登入後不重抓」；換帳號重登入亦重抓＝關閉跨帳號殘留清單。
    const handleAuthChanged = () => { refreshProjects(); };
    window.addEventListener('touka_auth_changed', handleAuthChanged);
    return () => window.removeEventListener('touka_auth_changed', handleAuthChanged);
  }, [refreshProjects]);

  return (
    <ProjectContext.Provider value={{ projects, refreshProjects }}>
      {children}
    </ProjectContext.Provider>
  );
}

export function useProjects() {
  const context = useContext(ProjectContext);
  if (!context) throw new Error("useProjects must be used within ProjectProvider");
  return context;
}
