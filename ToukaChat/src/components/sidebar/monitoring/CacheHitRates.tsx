import { useTranslation } from "react-i18next";
import type { CacheHitRateMap } from "@/hooks/useSystemMonitoring";

const KEY = "sidebar.cache_hit";
const KNOWN_PURPOSES = new Set(["summarizer", "critic", "judge", "auto_reviewer", "inline_completion"]);
const NO_DATA = "—";
const PERCENT = 100;

/** 近 7 天的快取命中率（ADR-0311）。比例由後端算好；鍵不一定是廠商，也可能是旁路用途或自訂連線。 */
export function CacheHitRates({ rates }: { rates: CacheHitRateMap }) {
  const { t } = useTranslation();
  const rows = Object.entries(rates ?? {}).flatMap(([agent, labels]) =>
    Object.entries(labels ?? {}).map(([label, rate]) => ({ agent, label, rate })),
  );
  if (rows.length === 0) return null;
  return (
    <div className="bg-background p-3 rounded-xl border border-border space-y-2">
      <p className="text-xs font-bold text-muted-foreground">{t(`${KEY}.title`)}</p>
      {rows.map(({ agent, label, rate }) => (
        <div
          key={`${agent}/${label}`}
          data-testid={`cache-hit-${agent}-${label}`}
          className="flex items-center justify-between text-xs gap-2"
        >
          <span className="text-muted-foreground truncate">
            <span className="font-bold uppercase">{agent}</span>
            {" · "}
            {KNOWN_PURPOSES.has(label) ? t(`${KEY}.labels.${label}`) : label}
          </span>
          <span className="font-mono text-primary font-bold whitespace-nowrap">
            {typeof rate === "number" ? `${Math.round(rate * PERCENT)}%` : NO_DATA}
          </span>
        </div>
      ))}
    </div>
  );
}
