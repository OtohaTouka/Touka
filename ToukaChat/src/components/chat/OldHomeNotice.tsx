import { useRef } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { useHomeConfirm } from "@/hooks/useHomeConfirm";
import { HomeConfirmDialog } from "./HomeConfirmDialog";

/** 舊家（ADR-0306）只能看：輸入框換成這段說明，要繼續就先設回家。 */
export function OldHomeNotice({ sessionId }: { sessionId: string }) {
  const { t } = useTranslation();
  const home = useHomeConfirm();
  const buttonRef = useRef<HTMLButtonElement>(null);
  return (
    <div
      role="status"
      data-testid="old-home-notice"
      className="border-border bg-muted/50 flex items-center justify-between gap-3 rounded-2xl border px-4 py-3"
    >
      <p className="text-muted-foreground min-w-0 text-sm">{t("chat.old_home.notice")}</p>
      <Button ref={buttonRef} size="sm" onClick={() => home.askSetHome(sessionId)} className="shrink-0 rounded-xl font-bold">
        {t("chat.old_home.set_as_home")}
      </Button>
      <HomeConfirmDialog request={home.request} onCancel={home.cancel} onConfirm={home.confirm}
        returnFocus={() => buttonRef.current?.focus()} />
    </div>
  );
}
