import { useTranslation } from "react-i18next";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { McpOAuthStatus as McpOAuthStatusValue } from "@/lib/mcp_version";
import { McpOAuthUiError } from "@/hooks/settings/useMcpOAuth";

interface Props {
  status: McpOAuthStatusValue;
  busy: boolean;
  error: McpOAuthUiError | null;
  onConnect: () => void;
  onDisconnect: () => void;
}

export function McpOAuthStatus({
  status,
  busy,
  error,
  onConnect,
  onDisconnect,
}: Props) {
  const { t } = useTranslation();
  const canConnect = status === "auth_required";
  const canDisconnect = status === "connected" || status === "frozen";

  return (
    <div className="flex flex-wrap items-center gap-2">
      <Badge variant="outline">
        {t(`settings.mcp_oauth.status_${status}`)}
      </Badge>
      {canConnect && (
        <Button size="sm" disabled={busy} onClick={onConnect}>
          {t("settings.mcp_oauth.connect")}
        </Button>
      )}
      {canDisconnect && (
        <Button
          variant="outline"
          size="sm"
          disabled={busy}
          onClick={onDisconnect}
        >
          {t("settings.mcp_oauth.disconnect")}
        </Button>
      )}
      {error && (
        <span role="alert" className="text-status-error text-xs font-bold">
          {t(`settings.mcp_oauth.error_${error}`)}
        </span>
      )}
    </div>
  );
}
