import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { useMcpIsolation } from "@/hooks/settings/useMcpIsolation";
import type { McpRestartRequest, McpServerStatus } from "@/lib/types";

const KEY = "settings.mcp_isolation.manual_start";

interface Props {
  server: McpServerStatus;
  onRestart: (key: string, ack?: McpRestartRequest) => Promise<boolean>;
  onClose: () => void;
}

export function McpManualStartDialog({ server, onRestart, onClose }: Props) {
  const { t, i18n } = useTranslation();
  const { view, error } = useMcpIsolation(server.name);
  const [submitting, setSubmitting] = useState(false);
  const sent = useRef(false);
  const plan = server.isolation?.manual_start;
  const destinationKnown =
    plan && ["executor", "landlock", "direct"].includes(plan.fallback);
  const featuresKnown = plan?.features.every(
    (feature) =>
      i18n.exists(`${KEY}.feature_names.${feature}`) && feature !== "unknown",
  );
  const changed = view && (view.class !== "self" || view.bound !== plan?.bound);
  const ready = destinationKnown && featuresKnown && view && !changed && !error;
  const confirm = async () => {
    if (!ready || !plan || sent.current) return;
    sent.current = true;
    setSubmitting(true);
    try {
      await onRestart(server.name, {
        isolation_ack: plan.fallback,
        features: plan.features,
        generation: plan.generation,
        bound: view.bound,
      });
    } finally {
      onClose();
    }
  };

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !sent.current) onClose();
      }}
    >
      <DialogContent className="bg-background border-border w-full max-w-lg space-y-4 rounded-2xl border p-6">
        <DialogTitle className="text-foreground font-bold">
          {t(`${KEY}.title`, { name: server.name })}
        </DialogTitle>
        <DialogDescription>{t(`${KEY}.once`)}</DialogDescription>
        <p className="text-status-warning whitespace-pre-wrap">
          {t(
            `${KEY}.destinations.${destinationKnown ? plan.fallback : "unknown"}`,
          )}
        </p>
        <p className="text-muted-foreground text-sm">
          {t(`${KEY}.features`, {
            features: plan?.features.length
              ? plan.features
                  .map((feature) =>
                    t(
                      `${KEY}.feature_names.${i18n.exists(`${KEY}.feature_names.${feature}`) ? feature : "unknown"}`,
                    ),
                  )
                  .join(t(`${KEY}.separator`))
              : t(`${KEY}.no_features`),
          })}
        </p>
        {plan && !plan.features.includes("tcp") && (
          <p className="text-status-warning text-sm">
            {t(`${KEY}.tcp_missing`)}
          </p>
        )}
        {(error || changed) && (
          <p role="alert" className="text-destructive text-sm">
            {error || t("settings.mcp_isolation.errors.mcp_entry_changed")}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button variant="outline" disabled={submitting} onClick={onClose}>
            {t(`${KEY}.cancel`)}
          </Button>
          <Button
            disabled={!ready || submitting}
            onClick={() => void confirm()}
          >
            {t(`${KEY}.confirm`)}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
