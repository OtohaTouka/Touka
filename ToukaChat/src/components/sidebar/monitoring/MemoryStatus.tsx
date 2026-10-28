import { MetricSmall, HealthTag } from "./StatusUtils";

interface MemoryStatusProps {
  memStats: { memory_load: number; status: string } | null;
  vaultHealth: Record<string, boolean> | null;
}

export function MemoryStatus({
  memStats,
  vaultHealth,
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
        <div className="flex flex-wrap gap-1.5 pt-1">
          <HealthTag label="Vector" active={!!vaultHealth?.vector} />
          <HealthTag label="Legacy" active={!!vaultHealth?.legacy} />
          <HealthTag label="Drive" active={!!vaultHealth?.personal} />
        </div>
      </div>
    </div>
  );
}
