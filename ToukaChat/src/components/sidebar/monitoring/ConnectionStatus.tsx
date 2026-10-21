import { cn } from "@/lib/utils";

interface ConnectionStatusProps {
  connected: boolean;
  latency: string;
}

export function ConnectionStatus({ connected, latency }: ConnectionStatusProps) {
  return (
    // WCAG 4.1.3：連線狀態文字（Connected/Disconnected）變化須讓螢幕閱讀器聽見
    <div role="status" className="bg-background p-3 rounded-xl border border-border">
      <div className="flex items-center gap-3 mb-2">
        <div
          className={cn(
            "w-2.5 h-2.5 rounded-full animate-pulse",
            connected
              ? "bg-status-success shadow-sm"
              : "bg-status-error"
          )}
        ></div>
        <span className="text-xs font-medium text-foreground">
          {connected ? "Connected to Host" : "Disconnected"}
        </span>
      </div>
      <div className="flex items-center justify-between text-xs">
        <span className="text-muted-foreground font-bold uppercase tracking-wider">
          Latency
        </span>
        <span
          className={cn(
            "font-mono font-bold",
            connected ? "text-status-success" : "text-status-error"
          )}
        >
          {latency}
        </span>
      </div>
    </div>
  );
}
