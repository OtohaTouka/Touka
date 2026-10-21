"use client";

import { useEffect, useState, useCallback } from 'react';
import { useArtifacts, MissionTask } from '@/hooks/useArtifacts';
import { useModels } from '@/lib/providers/ModelProvider';
import { getActiveProxyUrl, fetchTouka } from '@/lib/connections';
import {
  CheckCircle2, Circle, Loader2, AlertCircle, Target,
  ChevronRight, FileText, Activity, RefreshCcw, Bot
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { motion, AnimatePresence } from 'framer-motion';
import { logSSOTError, errorMsg } from '@/lib/error_handler';
import { getDispatchedTaskIds } from '@/lib/mission_utils';
import { useTranslation } from "react-i18next";

export default function MissionBoard() {
  const { t } = useTranslation();
  const { missionData, setMissionData, activeTab } = useArtifacts();
  const { selectedModel } = useModels();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchMission = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const proxyUrl = getActiveProxyUrl();
      
      let soulId = selectedModel;
      if (soulId.toLowerCase().startsWith("toukasoul")) {
          soulId = soulId.slice(9);
      }

      const res = await fetchTouka(proxyUrl, '/v1/ui/mission', {
          headers: {
              'X-Touka-Soul-ID': soulId
          }
      });
      if (res.ok) {
        const data = await res.json();
        setMissionData(data);
      } else if (res.status === 404) {
        setMissionData(null);
      } else {
        throw new Error(errorMsg("SYSTEM_ERROR"));
      }
    } catch (err) {
      logSSOTError(err, "MissionBoard:fetchMission");
      setError(errorMsg("SYSTEM_ERROR"));
    } finally {
      setLoading(false);
    }
  }, [selectedModel, setMissionData]);

  useEffect(() => {
    if (activeTab === 'mission') {
      fetchMission();
    }
  }, [activeTab, fetchMission]);

  useEffect(() => {
    const handleUpdate = () => {
        if (activeTab === 'mission') fetchMission();
    };
    window.addEventListener('touka_mission_updated', handleUpdate);
    return () => window.removeEventListener('touka_mission_updated', handleUpdate);
  }, [activeTab, fetchMission]);

  if (loading && !missionData) {
    return (
      <div className="flex flex-col items-center justify-center h-full p-8 text-muted-foreground animate-pulse">
        <Activity className="w-10 h-10 mb-4 text-primary" />
        <p className="text-sm font-bold uppercase tracking-widest">{t('artifacts.mission_board.loading')}</p>
      </div>
    );
  }

  if (error && !missionData) {
    return (
      <div className="flex flex-col items-center justify-center h-full p-8 text-center">
        <AlertCircle className="w-12 h-12 mb-4 text-status-error opacity-50" />
        <p className="text-sm text-status-error font-bold mb-4">{error}</p>
        <button 
          onClick={fetchMission}
          className="flex items-center gap-2 px-4 py-2 bg-primary/10 text-primary rounded-full text-xs font-black hover:bg-primary/20 transition-all"
        >
          <RefreshCcw className="w-3 h-3" /> {t('artifacts.mission_board.retry')}
        </button>
      </div>
    );
  }

  if (!missionData) {
    return (
      <div className="flex flex-col items-center justify-center h-full p-8 text-center text-muted-foreground">
        <Target className="w-16 h-12 mb-4 opacity-10" />
        <h3 className="text-sm font-black uppercase tracking-tighter mb-2">{t('artifacts.mission_board.empty_title')}</h3>
        <p className="text-xs opacity-60 w-48">
          {t('artifacts.mission_board.empty_description')}
        </p>
      </div>
    );
  }

  const completedCount = missionData.tasks.filter((t: MissionTask) => t.status === 'DONE').length;
  const progress = (completedCount / missionData.tasks.length) * 100;
  // P4b：已分派給外部 AI（等待回呼）的任務 id —— 在卡片上標示「外部 AI」
  const dispatchedIds = getDispatchedTaskIds(missionData.metadata);

  return (
    <div className="flex flex-col h-full bg-sidebar">
      <div className="p-6 border-b border-border bg-background/50 backdrop-blur-md sticky top-0 z-10">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <div className="p-1.5 bg-primary/10 rounded-lg">
              <Target className="w-4 h-4 text-primary" />
            </div>
            <span className="text-xs font-black uppercase tracking-widest text-muted-foreground">Mission Control</span>
          </div>
          <button onClick={fetchMission} className="p-1 hover:text-primary transition-colors">
            <RefreshCcw className={cn("w-3.5 h-3.5", loading && "animate-spin")} />
          </button>
        </div>

        <h2 className="text-lg font-black tracking-tight mb-4 text-foreground leading-tight">
          {missionData.goal}
        </h2>

        <div className="space-y-2">
          <div className="flex justify-between items-end text-xs font-black uppercase tracking-widest text-muted-foreground">
            <span>Progress</span>
            <span className="text-primary">{Math.round(progress)}%</span>
          </div>
          <div className="h-2 w-full bg-muted rounded-full overflow-hidden border border-border/50">
            <motion.div 
              className="h-full bg-gradient-to-r from-primary to-primary/60"
              initial={{ width: 0 }}
              animate={{ width: `${progress}%` }}
              transition={{ duration: 1, ease: "circOut" }}
            />
          </div>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto p-4 custom-scrollbar space-y-3">
        <AnimatePresence mode="popLayout">
          {missionData.tasks.map((task: MissionTask, index: number) => (
            <motion.div
              key={task.id}
              initial={{ opacity: 0, x: -20 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ delay: index * 0.1 }}
              className={cn(
                "group relative p-4 rounded-xl border transition-all duration-300",
                task.status === 'DONE' 
                  ? "bg-muted/30 border-border/40" 
                  : task.status === 'DOING'
                  ? "bg-primary/5 border-primary/20 ring-1 ring-primary/10 shadow-lg shadow-primary/5"
                  : "bg-background border-border hover:border-primary/30"
              )}
            >
              <div className="flex items-start gap-4">
                <div className="mt-0.5 shrink-0">
                  {task.status === 'DONE' ? (
                    <CheckCircle2 className="w-5 h-5 text-status-success" />
                  ) : task.status === 'DOING' ? (
                    <div className="relative">
                       <Loader2 className="w-5 h-5 text-primary animate-spin" />
                       <div className="absolute inset-0 bg-primary/20 blur-md rounded-full animate-pulse" />
                    </div>
                  ) : task.status === 'FAILED' ? (
                    <AlertCircle className="w-5 h-5 text-destructive" />
                  ) : (
                    <Circle className="w-5 h-5 text-muted-foreground" />
                  )}
                </div>

                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1">
                    <span className="text-xs font-black text-primary/60 uppercase tracking-tighter tabular-nums">#{task.id}</span>
                    {dispatchedIds.has(task.id) && (
                      <span
                        className="inline-flex items-center gap-1 text-xs font-bold text-primary uppercase"
                        title={t('artifacts.mission_board.dispatched_external_ai_tooltip')}
                      >
                        <Bot className="w-3 h-3" />
                        {t('artifacts.mission_board.external_ai')}
                      </span>
                    )}
                    {task.dependencies && task.dependencies.length > 0 && (
                      <span className="text-xs text-muted-foreground uppercase font-bold">Needs: {task.dependencies.join(', ')}</span>
                    )}
                  </div>
                  <p className={cn(
                    "text-sm font-bold leading-snug break-words transition-colors",
                    task.status === 'DONE' ? "text-muted-foreground line-through opacity-60" : "text-foreground"
                  )}>
                    {task.description}
                  </p>
                  
                  {(task.artifact_ref || (task.artifact_refs && task.artifact_refs.length > 0)) && (
                    <div className="mt-3 flex flex-wrap items-center gap-2">
                      {task.artifact_ref && (
                        <div className="flex items-center gap-1.5 px-2 py-1 rounded bg-muted border border-border hover:border-primary/40 hover:bg-primary/5 transition-all cursor-pointer group/art">
                          <FileText className="w-3 h-3 text-muted-foreground group-hover/art:text-primary" />
                          <span className="text-xs font-mono text-muted-foreground group-hover/art:text-primary truncate w-40">
                            {task.artifact_ref}
                          </span>
                          <ChevronRight className="w-3 h-3 text-muted-foreground group-hover/art:text-primary ml-1" />
                        </div>
                      )}
                      {task.artifact_refs && task.artifact_refs.map((ref) => (
                        <div key={ref} className="flex items-center gap-1.5 px-2 py-1 rounded bg-muted border border-border hover:border-primary/40 hover:bg-primary/5 transition-all cursor-pointer group/art">
                          <FileText className="w-3 h-3 text-muted-foreground group-hover/art:text-primary" />
                          <span className="text-xs font-mono text-muted-foreground group-hover/art:text-primary truncate w-40">
                            {ref}
                          </span>
                          <ChevronRight className="w-3 h-3 text-muted-foreground group-hover/art:text-primary ml-1" />
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            </motion.div>
          ))}
        </AnimatePresence>
      </div>

      <div className="p-4 border-t border-border bg-muted/20 text-xs font-bold text-muted-foreground uppercase tracking-widest flex justify-between">
        <span>Mission ID: {missionData.mission_id.slice(0, 8)}</span>
        <span>Touka Autonomy System</span>
      </div>
    </div>
  );
}
