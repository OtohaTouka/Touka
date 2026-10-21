"use client";

import { useState, useEffect } from "react";
import { useDebug } from "@/lib/providers/DebugProvider";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import {
  Bug, Play, Database, MessageSquare,
  Terminal, Wand2, Calculator, Trophy, Layers
} from "lucide-react";
import { cn } from "@/lib/utils";
import { fetchToolSchemas, fetchTouka, getActiveProxyUrl } from "@/lib/connections";
import { getSystemEpoch } from "@/lib/time_utils";
import { logSSOTError, errorMsg } from "@/lib/error_handler";
import { toast } from "sonner";
import DynamicToolForm from "../debug/DynamicToolForm";
import { ScorecardPanel } from "./ScorecardPanel";
import { ContextSnapshotPanel } from "./ContextSnapshotPanel";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import type { components } from "@/types/api";

type ToolExecuteRequest = components["schemas"]["ToolExecuteRequest"];

interface ToolDefinition {
  function: {
    name: string;
    description?: string;
    parameters?: Record<string, unknown>;
  };
}

export function DebugMonitoringView() {
  const { 
    lastRequestPayload, 
    isSimulationMode,
    emitSimulatedChunk,
    emitSimulatedEvent,
    finishSimulation
  } = useDebug();

  const [simText, setSimText] = useState("");
  const [tools, setTools] = useState<ToolDefinition[]>([]);
  const [selectedToolId, setSelectedToolId] = useState<string | null>(null);
  const [formValues, setFormValues] = useState<Record<string, unknown>>({});
  const [isManualMode, setIsManualMode] = useState(false);
  const [toolName, setToolName] = useState("");
  const [toolArgs, setToolArgs] = useState("{}");
  const [isExecuting, setIsExecuting] = useState(false);

  useEffect(() => {
    fetchToolSchemas().then(schemas => setTools(schemas as unknown as ToolDefinition[])).catch(e => logSSOTError(e, "DebugMonitoringView:fetchTools"));
  }, []);

  const selectedTool = tools.find((t) => t.function.name === selectedToolId);

  const handleSendChunk = () => {
    if (simText) {
      emitSimulatedChunk(simText);
      setSimText("");
    }
  };

  const handleToolStart = async () => {
    const activeToolName = isManualMode ? toolName : selectedToolId;
    if (!activeToolName) return;
    
    try {
      const args = isManualMode ? JSON.parse(toolArgs) : formValues;
      const callId = `sim-${getSystemEpoch()}`;
      
      emitSimulatedEvent({
        type: "tool_start",
        id: callId,
        tool: activeToolName,
        args
      });

      setIsExecuting(true);

      const proxyUrl = getActiveProxyUrl();
      const payload = {
        tool_name: activeToolName,
        args,
        agent_id: "DebugAgent"  // API accepts string ID, typed as AgentIdentity in schema
      } as unknown as ToolExecuteRequest;

      const response = await fetchTouka(proxyUrl, "/v1/debug/execute", {
        method: "POST",
        body: JSON.stringify(payload)
      });

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${await response.text()}`);
      }

      const data = await response.json();

      emitSimulatedEvent({
        type: "tool_result",
        id: callId,
        result: data.result,
        status: data.status === "success" ? "success" : "error"
      });

    } catch (e) {
      logSSOTError(e, "DebugMonitoringView:handleToolStart");
      toast.error(errorMsg("SYSTEM_ERROR"));
    } finally {
      setIsExecuting(false);
    }
  };

  return (
    <div className="flex flex-col gap-4 w-full min-w-0 p-4">
      <div className="flex items-center gap-3 mb-2 -translate-y-1">
        <div className="p-2 bg-primary/10 rounded-lg shrink-0">
          <Bug className="h-5 w-5 text-primary" />
        </div>
        <div className="text-left min-w-0">
          <h3 className="truncate text-sm font-bold tracking-tight">Touka Debug Center</h3>
          <p className="text-xs font-medium tracking-widest uppercase opacity-70">
            Virtual Debug Mode v1.2
          </p>
        </div>
        {isSimulationMode && (
          <Badge variant="destructive" className="ml-auto h-4 shrink-0 animate-pulse border-none px-1.5 text-xs font-bold">
            INTERCEPTED
          </Badge>
        )}
      </div>

      <Tabs defaultValue="simulation" className="flex-col gap-4">
        <TabsList className="grid grid-cols-4 bg-muted/30">
          <TabsTrigger value="simulation" className="text-xs font-semibold">
            <Play className="h-3.5 w-3.5 mr-1.5" /> Sim
          </TabsTrigger>
          <TabsTrigger value="payload" className="text-xs font-semibold">
            <Database className="h-3.5 w-3.5 mr-1.5" /> Payload
          </TabsTrigger>
          <TabsTrigger value="context" className="text-xs font-semibold">
            <Layers className="h-3.5 w-3.5 mr-1.5" /> Context
          </TabsTrigger>
          <TabsTrigger value="scorecard" className="text-xs font-semibold">
            <Trophy className="h-3.5 w-3.5 mr-1.5" /> Score
          </TabsTrigger>
        </TabsList>

        <TabsContent value="payload" className="mt-4 space-y-4 focus-visible:outline-none">
          <div className="flex items-center gap-2 mb-2">
            <Terminal className="h-3.5 w-3.5 text-primary" />
            <h3 className="text-xs font-bold">Last Request</h3>
          </div>
          <Card className="border-border bg-muted/50 shadow-inner overflow-hidden">
            <CardContent className="p-3">
              <pre className="font-mono text-xs leading-relaxed text-primary break-all whitespace-pre-wrap">
                {lastRequestPayload 
                  ? JSON.stringify(lastRequestPayload, null, 2) 
                  : "// No data yet"}
              </pre>
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="context" className="mt-4 focus-visible:outline-none">
          <ContextSnapshotPanel />
        </TabsContent>

        <TabsContent value="scorecard" className="mt-4 focus-visible:outline-none">
          <ScorecardPanel />
        </TabsContent>

        <TabsContent value="simulation" className="mt-4 space-y-6 focus-visible:outline-none">
          <div className="space-y-3">
            <h3 className="text-xs font-bold flex items-center gap-2 text-muted-foreground">
              <MessageSquare className="h-3.5 w-3.5" /> Response Sim
            </h3>
            <Textarea 
              placeholder="Enter response chunk..."
              value={simText}
              onChange={(e) => setSimText(e.target.value)}
              className="text-xs min-h-20 bg-muted/20 border-primary/5"
            />
            <div className="flex gap-2">
              <Button 
                size="sm" 
                className="flex-1 font-bold h-8" 
                onClick={handleSendChunk}
                disabled={!isSimulationMode}
              >
                Send
              </Button>
              <Button 
                size="sm" 
                variant="outline" 
                className="flex-1 font-bold h-8"
                onClick={finishSimulation}
                disabled={!isSimulationMode}
              >
                Finish
              </Button>
            </div>
          </div>

          <Separator className="bg-primary/5" />

          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <h3 className="text-xs font-bold flex items-center gap-2 text-muted-foreground">
                <Wand2 className="h-3.5 w-3.5" /> Tool Injection
              </h3>
              <div className="flex items-center gap-2 rounded-full border border-primary/5 bg-muted/20 px-2 py-1">
                <Label htmlFor="manual-mode-sidebar" className="text-xs font-bold">MANUAL</Label>
                <Checkbox 
                  id="manual-mode-sidebar" 
                  checked={isManualMode} 
                  onCheckedChange={(c) => setIsManualMode(!!c)} 
                  className="h-3.5 w-3.5"
                />
              </div>
            </div>

            {!isManualMode ? (
              <div className="space-y-4">
                <Select
                  value={selectedToolId || ""}
                  onValueChange={setSelectedToolId}
                >
                  <SelectTrigger className="h-9 text-xs bg-muted/20 border-primary/5 w-full">
                    <SelectValue placeholder="Select Tool..." />
                  </SelectTrigger>
                  <SelectContent className="max-h-72">
                    {tools.map(t => (
                      <SelectItem key={t.function.name} value={t.function.name}>
                        <span className="font-bold text-primary">{t.function.name}</span>
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>

                {selectedTool && (
                  <Card className="border-primary/10 bg-card overflow-hidden">
                    <CardHeader className="bg-muted/30 p-2 flex flex-row items-center justify-between space-y-0">
                      <CardTitle className="flex items-center gap-1.5 text-xs font-bold text-primary">
                        <Calculator className="h-3 w-3" /> PARAMS
                      </CardTitle>
                      <Button 
                        size="sm" 
                        className="h-6 px-2 text-xs font-bold" 
                        onClick={handleToolStart} 
                        disabled={!isSimulationMode || isExecuting}
                      >
                        {isExecuting ? "Executing" : "Execute"}
                      </Button>
                    </CardHeader>
                    <CardContent className="p-3 pt-4 min-w-0">
                      <DynamicToolForm schema={selectedTool} onChange={setFormValues} />
                    </CardContent>
                  </Card>
                )}
              </div>
            ) : (
              <div className="space-y-4 p-3 border rounded-xl bg-muted/10 border-primary/5">
                <div className="flex gap-2">
                  <Input 
                    placeholder="Tool Name" 
                    value={toolName}
                    onChange={(e) => setToolName(e.target.value)}
                    className="text-xs h-8 bg-muted/20"
                  />
                  <Button 
                    size="sm" 
                    variant="secondary" 
                    className="h-8 px-3 text-xs font-bold" 
                    onClick={handleToolStart} 
                    disabled={!isSimulationMode || isExecuting}
                  >
                    Trigger
                  </Button>
                </div>
                <Textarea 
                  placeholder="JSON args..."
                  value={toolArgs}
                  onChange={(e) => setToolArgs(e.target.value)}
                  className="min-h-20 border-none bg-muted/50 font-mono text-xs text-primary"
                />
              </div>
            )}
          </div>
          
          <div className="p-4 border-t bg-muted/30 mt-auto">
            <div className="flex justify-between items-center h-5">
              <div className="flex items-center gap-2">
                <div className={cn(
                  "h-2 w-2 rounded-full", 
                  isSimulationMode ? "animate-pulse bg-status-error shadow-sm" : "bg-status-success shadow-sm"
                )} />
                <span className="tabular-nums text-xs font-bold tracking-tighter uppercase text-muted-foreground">
                  {isSimulationMode ? "Intercepting" : "Standby"}
                </span>
              </div>
              <Badge variant="outline" className="h-5 border-primary/10 bg-background/50 px-2 text-xs">v1.2.0</Badge>
            </div>
          </div>
        </TabsContent>
      </Tabs>
    </div>
  );
}
