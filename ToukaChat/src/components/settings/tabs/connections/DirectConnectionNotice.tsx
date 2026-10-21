import { useTranslation } from "react-i18next";
import { Info } from "lucide-react";

/** P10：Touka Proxy 以外的連線是瀏覽器直連——沒有工具、不受權限模式管、不記帳。 */
export function DirectConnectionNotice({ provider }: { provider: string }) {
  const { t } = useTranslation();
  if (provider === "proxy") return null;
  return (
    <p role="note" className="mt-1 flex items-start gap-1.5 rounded-lg bg-status-warning/10 p-2 text-xs text-status-warning">
      <Info size={12} className="mt-0.5 shrink-0" />
      <span>{t("connections.providers.direct_notice")}</span>
    </p>
  );
}
