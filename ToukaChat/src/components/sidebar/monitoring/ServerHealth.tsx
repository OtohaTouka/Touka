import { StatusProgress } from "./StatusUtils";

interface ServerHealthProps {
  healthStats: { cpu_usage: number; memory_usage: number } | null;
}

export function ServerHealth({ healthStats }: ServerHealthProps) {
  return (
    <div className="bg-background p-3 rounded-xl border border-border space-y-3">
      <StatusProgress
        label="CPU Load"
        progress={healthStats?.cpu_usage || 0}
      />
      <StatusProgress
        label="RAM Usage"
        progress={healthStats?.memory_usage || 0}
      />
    </div>
  );
}
