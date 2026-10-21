import { DailyQuotaStats } from "@/hooks/useSystemMonitoring";

interface DailyQuotaProps {
  quota: DailyQuotaStats | null;
}

/** 今日自主配額：顯示本人今日已用 / 上限 / 剩餘（資料來自 /stats 的 daily_quota）。 */
export function DailyQuota({ quota }: DailyQuotaProps) {
  if (!quota) return null;
  return (
    <div className="bg-background p-3 rounded-xl border border-border">
      <div className="flex items-center justify-between text-xs">
        <span className="font-bold text-muted-foreground uppercase">Daily Quota</span>
        <span className="font-mono text-primary font-bold">
          {quota.daily_count}/{quota.daily_max}
          <span className="text-muted-foreground font-normal"> · {quota.remaining} left</span>
        </span>
      </div>
    </div>
  );
}
