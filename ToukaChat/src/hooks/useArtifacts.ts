import { create } from 'zustand';

export type ArtifactType = 'web' | 'react' | 'mermaid' | 'markdown' | 'code';

export interface Artifact {
  id: string;
  title: string;
  type: ArtifactType;
  content: string;
  language?: string;
}

export interface MissionTask {
  id: string;
  description: string;
  status: 'TODO' | 'DOING' | 'DONE' | 'FAILED' | 'BLOCKED';
  dependencies: string[];
  artifact_ref?: string;
  artifact_refs?: string[];
}

export interface MissionData {
  mission_id: string;
  goal: string;
  agent_id: string;
  status: string;
  tasks: MissionTask[];
  // 後端 /v1/ui/mission 的 model_dump 含 metadata；dispatch_callbacks: task_id -> callback_token
  // （= 已分派給外部 AI、等待回呼的任務）。
  metadata?: { dispatch_callbacks?: Record<string, string> };
}

interface ArtifactState {
  artifacts: Artifact[];
  currentArtifactId: string | null;
  isOpen: boolean;
  activeTab: 'system' | 'artifacts' | 'project' | 'mission' | 'debug';
  missionData: MissionData | null;
  
  // Actions
  addArtifact: (artifact: Omit<Artifact, 'id'>) => string;
  setCurrentArtifact: (id: string) => void;
  setIsOpen: (open: boolean) => void;
  setActiveTab: (tab: 'system' | 'artifacts' | 'project' | 'mission' | 'debug') => void;
  setMissionData: (data: MissionData | null) => void;
  updateArtifact: (id: string, content: string) => void;
  clearArtifacts: (scopeReset?: boolean) => void;
  loadArtifacts: (artifacts: Omit<Artifact, 'id'>[]) => void;
  isManuallyClosed: boolean;
  setManuallyClosed: (closed: boolean) => void;
}

export const useArtifacts = create<ArtifactState>((set) => ({
  artifacts: [],
  currentArtifactId: null,
  isOpen: false,
  activeTab: 'system',
  missionData: null,
  isManuallyClosed: false,

  addArtifact: (artifactData) => {
    const id = Math.random().toString(36).substring(2, 9);
    const newArtifact = { ...artifactData, id };
    
    set((state) => ({
      artifacts: [newArtifact, ...state.artifacts],
      currentArtifactId: id,
      isOpen: state.isManuallyClosed ? state.isOpen : true,
      activeTab: 'artifacts',
    }));
    
    return id;
  },

  setCurrentArtifact: (id) => set((state) => ({ 
    currentArtifactId: id, 
    activeTab: 'artifacts', 
    isOpen: state.isManuallyClosed ? state.isOpen : true 
  })),
  setIsOpen: (open) => set({ isOpen: open, isManuallyClosed: !open }),
  setActiveTab: (tab) => set({ activeTab: tab }),
  setMissionData: (data) => set({ missionData: data }),
  updateArtifact: (id, content) => set((state) => ({
    artifacts: state.artifacts.map(a => a.id === id ? { ...a, content } : a)
  })),
  clearArtifacts: (scopeReset = false) => set({
    artifacts: [], currentArtifactId: null,
    ...(scopeReset ? { missionData: null, isOpen: false, activeTab: 'system' as const, isManuallyClosed: false } : {}),
  }),
  loadArtifacts: (newArtifacts) => {
    const mappedArtifacts = newArtifacts.map(a => ({ ...a, id: Math.random().toString(36).substring(2, 9) }));
    set({ 
      artifacts: mappedArtifacts,
      currentArtifactId: mappedArtifacts.length > 0 ? mappedArtifacts[0].id : null,
    });
  },
  setManuallyClosed: (closed) => set({ isManuallyClosed: closed }),
}));
