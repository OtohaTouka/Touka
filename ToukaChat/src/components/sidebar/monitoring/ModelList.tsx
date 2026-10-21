
interface ModelListProps {
  models: Array<{ id: string; owned_by: string }>;
}

export function ModelList({ models }: ModelListProps) {
  return (
    <div className="bg-background p-3 rounded-xl border border-border">
      <div className="custom-scrollbar max-h-40 overflow-x-hidden overflow-y-auto space-y-2">
        {models.length > 0 ? (
          models.map((m, i) => (
            <div
              key={i}
              className="flex justify-between items-center border-b border-border pb-1.5 last:border-0 last:pb-0"
            >
              <span
                className="mr-2 truncate text-xs text-muted-foreground"
                title={m.id}
              >
                {m.id}
              </span>
              <span className="rounded bg-accent px-1.5 py-0.5 font-mono text-xs text-muted-foreground">
                {m.owned_by}
              </span>
            </div>
          ))
        ) : (
          <p className="text-xs italic text-muted-foreground">
            No models detected
          </p>
        )}
      </div>
    </div>
  );
}
