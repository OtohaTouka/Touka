import { cn } from "@/lib/utils";

export function MetricSmall({
  label,
  value,
  color,
}: {
  label: string;
  value: string;
  color?: string;
}) {
  return (
    <div className="flex flex-col">
      <span className="text-xs font-bold uppercase text-muted-foreground">
        {label}
      </span>
      <span className={cn("text-xs font-bold", color || "text-foreground")}>
        {value}
      </span>
    </div>
  );
}

export function HealthTag({ label, active }: { label: string; active: boolean }) {
  return (
    <div
      className={cn(
        "rounded border px-1.5 py-0.5 text-xs font-black uppercase transition-all",
        active
          ? "bg-primary/10 text-primary border-primary/20"
          : "bg-destructive/10 text-destructive border-destructive/20 opacity-60"
      )}
    >
      {label}
    </div>
  );
}

export function StatusProgress({
  label,
  progress,
  className = "",
}: {
  label: string;
  progress: number;
  className?: string;
}) {
  return (
    <div className={cn("space-y-1", className)}>
      <div className="flex items-center justify-between text-xs">
        <span className="text-muted-foreground">{label}</span>
        <span className="font-bold text-primary">{progress}%</span>
      </div>
      <div className="w-full bg-muted rounded-full h-1 overflow-hidden">
        <div
          className="bg-primary/60 h-full rounded-full transition-all duration-1000"
          style={{ width: `${progress}%` }}
        ></div>
      </div>
    </div>
  );
}
