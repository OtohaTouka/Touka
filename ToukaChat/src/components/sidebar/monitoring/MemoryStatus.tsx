import { Zap } from "lucide-react";
import { MetricSmall, HealthTag, StatusProgress } from "./StatusUtils";

interface MemoryStatusProps {
  memStats: { memory_load: number; status: string } | null;
  vaultHealth: Record<string, boolean> | null;
  emergence: { e_index: number; trend: string } | null;
}

export function MemoryStatus({
  memStats,
  vaultHealth,
  emergence,
}: MemoryStatusProps) {
  return (
    <div className="bg-background p-3 rounded-xl border border-border space-y-3">
      <div className="grid grid-cols-2 gap-2">
        <MetricSmall label="Fact Nodes" value={`${memStats?.memory_load || 0} nodes`} />
        <MetricSmall
          label="Sync"
          value={vaultHealth?.facts ? "Active" : "Offline"}
          color={vaultHealth?.facts ? "text-status-success" : "text-status-warning"}
        />
      </div>
      <div className="space-y-2 pt-2 border-t border-border/50">
        <h4 className="flex items-center gap-1.5 text-xs font-black uppercase tracking-widest text-muted-foreground">
          <Zap className="h-3 w-3 text-primary" /> Emergence Index
        </h4>
        <StatusProgress
          label={`E-Index (${emergence?.trend || "Stable"})`}
          progress={(emergence?.e_index || 0) * 100}
        />

        <div className="flex flex-wrap gap-1.5 pt-1">
          <HealthTag label="Vector" active={!!vaultHealth?.vector} />
          <HealthTag label="Legacy" active={!!vaultHealth?.legacy} />
          <HealthTag label="Drive" active={!!vaultHealth?.personal} />
        </div>
      </div>
    </div>
  );
}
