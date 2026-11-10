import { useTranslation } from "react-i18next";
import type { PricingStatus } from "@/hooks/useSystemMonitoring";

const KEY = "sidebar.pricing_note";

/** 費用估算的依據（ADR-0311）：價目表查證日期、自己改過的價格、用該家最高價估的模型。 */
export function PricingNote({ pricing }: { pricing: PricingStatus | null }) {
  const { t } = useTranslation();
  if (typeof pricing?.verified_at !== "string") return null;
  const estimated = Array.isArray(pricing.estimated_models) ? pricing.estimated_models : [];
  return (
    <div data-testid="pricing-note" className="text-xs text-muted-foreground space-y-1 px-1">
      <p>
        {t(`${KEY}.verified_at`, { date: pricing.verified_at })}
        {pricing.has_override ? t(`${KEY}.override`) : ""}
      </p>
      {estimated.length > 0 && (
        <p data-testid="pricing-estimated">
          {t(`${KEY}.estimated`, { models: estimated.join(t(`${KEY}.separator`)) })}
        </p>
      )}
    </div>
  );
}
