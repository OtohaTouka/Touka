import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";

interface Props {
  authorizationServers: string[];
  disabled: boolean;
  onSelect: (authorizationServer: string) => void;
}

function issuerLabel(issuer: string): string {
  try {
    return new URL(issuer).host;
  } catch {
    return issuer;
  }
}

export function McpOAuthSelection({
  authorizationServers,
  disabled,
  onSelect,
}: Props) {
  const { t } = useTranslation();

  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="text-muted-foreground text-xs font-bold">
        {t("settings.mcp_oauth.select_authorization_server")}
      </span>
      {authorizationServers.map((issuer) => {
        const label = issuerLabel(issuer);
        return (
          <Button
            key={issuer}
            variant="outline"
            size="sm"
            disabled={disabled}
            aria-label={t("settings.mcp_oauth.use_authorization_server", {
              issuer: label,
            })}
            title={issuer}
            onClick={() => onSelect(issuer)}
          >
            {label}
          </Button>
        );
      })}
    </div>
  );
}
