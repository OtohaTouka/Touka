import { TokenUsageMap } from "@/hooks/useSystemMonitoring";

interface TokenUsageProps {
  tokenUsage: TokenUsageMap;
}

/**
 * 累計 Token 面板：消費後端 /stats 的巢狀形狀（agent -> provider -> 用量），
 * 每個 agent 顯示「總 token（prompt+completion 跨 provider 加總）· 估算成本」。
 */
export function TokenUsage({ tokenUsage }: TokenUsageProps) {
  const fmt = new Intl.NumberFormat("en-US");
  const agents = Object.entries(tokenUsage || {});

  return (
    <div className="bg-background p-3 rounded-xl border border-border space-y-2">
      {agents.map(([agent, providers]) => {
        const stats = Object.values(providers || {});
        const totalTokens = stats.reduce(
          (sum, p) => sum + (p.prompt_tokens ?? 0) + (p.completion_tokens ?? 0),
          0,
        );
        const totalCost = stats.reduce((sum, p) => sum + (p.total_cost_usd ?? 0), 0);
        return (
          <div key={agent} className="flex items-center justify-between text-xs gap-2">
            <span className="font-bold text-muted-foreground uppercase truncate">{agent}</span>
            <span className="font-mono text-primary font-bold whitespace-nowrap">
              {fmt.format(totalTokens)}
              <span className="text-muted-foreground font-normal"> tok · ${totalCost.toFixed(4)}</span>
            </span>
          </div>
        );
      })}
      {agents.length === 0 && (
        <p className="text-xs italic text-muted-foreground">No usage recorded</p>
      )}
    </div>
  );
}
