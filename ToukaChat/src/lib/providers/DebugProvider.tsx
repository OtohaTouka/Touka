"use client";

import React, { createContext, useContext, useState, useCallback } from "react";

interface DebugContextType {
  isDebugPanelOpen: boolean;
  setIsDebugPanelOpen: (open: boolean) => void;
  lastRequestPayload: Record<string, unknown> | null;
  setLastRequestPayload: (payload: Record<string, unknown>) => void;
  isSimulationMode: boolean;
  setIsSimulationMode: (mode: boolean) => void;
  // 模擬事件發射器 (供 UI 呼叫)
  emitSimulatedChunk: (chunk: string) => void;
  emitSimulatedEvent: (event: Record<string, unknown>) => void;
  
  // 註冊 Handler (供 ChatProvider 註冊)
  registerSimHandlers: (handlers: {
    onChunk: (chunk: string) => void;
    onEvent: (event: Record<string, unknown>) => void;
    onDone: () => void;
  }) => void;
  finishSimulation: () => void;
}

const DebugContext = createContext<DebugContextType | undefined>(undefined);

export function DebugProvider({ children }: { children: React.ReactNode }) {
  const [isDebugPanelOpen, setIsDebugPanelOpen] = useState(false);
  const [lastRequestPayload, setLastRequestPayload] = useState<Record<string, unknown> | null>(null);
  const [isSimulationMode, setIsSimulationMode] = useState(false);
  
  const handlersRef = React.useRef<{
    onChunk: (chunk: string) => void;
    onEvent: (event: Record<string, unknown>) => void;
    onDone: () => void;
  } | null>(null);

  const registerSimHandlers = useCallback((handlers: {
    onChunk: (chunk: string) => void;
    onEvent: (event: Record<string, unknown>) => void;
    onDone: () => void;
  }) => {
    handlersRef.current = handlers;
  }, []);

  const emitSimulatedChunk = useCallback((chunk: string) => {
    if (handlersRef.current) {
      handlersRef.current.onChunk(chunk);
    }
  }, []);

  const emitSimulatedEvent = useCallback((event: Record<string, unknown>) => {
    if (handlersRef.current) {
      handlersRef.current.onEvent(event);
    }
  }, []);

  const finishSimulation = useCallback(() => {
    if (handlersRef.current) {
      handlersRef.current.onDone();
    }
    setIsSimulationMode(false);
  }, []);

  return (
    <DebugContext.Provider value={{
      isDebugPanelOpen,
      setIsDebugPanelOpen,
      lastRequestPayload,
      setLastRequestPayload,
      isSimulationMode,
      setIsSimulationMode,
      emitSimulatedChunk,
      emitSimulatedEvent,
      registerSimHandlers,
      finishSimulation
    }}>
      {children}
    </DebugContext.Provider>
  );
}

export function useDebug() {
  const context = useContext(DebugContext);
  if (context === undefined) {
    throw new Error("useDebug must be used within a DebugProvider");
  }
  return context;
}
