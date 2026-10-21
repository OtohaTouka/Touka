import { useTranslation } from "react-i18next";
import { Box, ListChecks, Settings2, X, RotateCw } from "lucide-react";
import { cn } from "@/lib/utils";
import { McpServerStatus } from "@/lib/types";
import { InstalledMcpConfig } from "@/lib/mcp_version";
import { ERRORS, type ErrorCode } from "@/lib/error_codes";
import { errorMsg } from "@/lib/error_handler";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { useMcpOAuth } from "@/hooks/settings/useMcpOAuth";
import { McpOAuthSelection } from "./McpOAuthSelection";
import { McpOAuthStatus } from "./McpOAuthStatus";
import { McpIsolationBadge } from "./McpIsolationBadge";

interface Props {
  serverKey: string;
  serverCfg: InstalledMcpConfig[string];
  isRunning: McpServerStatus | undefined;
  onToggle: (key: string) => void;
  onEdit: (key: string) => void;
  onDelete: (key: string) => void;
  onRestart: (key: string) => void;
  /** 開啟 per-installation 工具治理面板（ADR-0195 軸 A）。 */
  onManageTools: (key: string) => void;
  isRestarting?: boolean;
}

// 健康狀態 → 設計權杖配色（無 hex/任意值；對齊 ModelsTab healthColor）。
const STATUS_STYLE: Record<
  string,
  { dot: string; text: string; icon: string }
> = {
  connected: {
    dot: "bg-status-success",
    text: "text-status-success",
    icon: "text-primary",
  },
  circuit_open: {
    dot: "bg-status-warning",
    text: "text-status-warning",
    icon: "text-status-warning",
  },
  error: {
    dot: "bg-status-error",
    text: "text-status-error",
    icon: "text-status-error",
  },
  not_started: {
    dot: "bg-muted-foreground",
    text: "text-muted-foreground",
    icon: "text-muted-foreground",
  },
  disabled: {
    dot: "bg-muted-foreground",
    text: "text-muted-foreground",
    icon: "text-muted-foreground",
  },
};

export function McpServerItem({
  serverKey,
  serverCfg,
  isRunning,
  onToggle,
  onEdit,
  onDelete,
  onRestart,
  onManageTools,
  isRestarting,
}: Props) {
  const { t } = useTranslation();
  const isEnabled = serverCfg.enabled !== false;
  const isOAuth = serverCfg.auth_mode === "oauth_user";
  const oauth = useMcpOAuth(serverKey, isOAuth && isEnabled);
  const displayStatus = !isEnabled
    ? "disabled"
    : (isRunning?.status ?? "not_started");
  const s = STATUS_STYLE[displayStatus] ?? STATUS_STYLE.not_started;
  const showError =
    isEnabled &&
    (displayStatus === "error" || displayStatus === "circuit_open") &&
    isRunning?.last_error;
  const lastError = isRunning?.last_error;
  const errorText =
    lastError && Object.hasOwn(ERRORS, lastError)
      ? errorMsg(lastError as ErrorCode)
      : lastError;

  return (
    <div className="bg-muted/40 border-border group hover:border-primary/30 flex items-center justify-between rounded-2xl border p-5 transition-all">
      <div className="flex min-w-0 items-center gap-4">
        <div className="bg-background border-border flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border shadow-sm">
          <Box className={cn("transition-colors", s.icon)} />
        </div>
        <div className="min-w-0">
          <div className="text-foreground flex flex-wrap items-center gap-2 text-sm font-black">
            <span className="truncate">{serverKey}</span>
            {displayStatus === "connected" && (
              <Badge
                variant="secondary"
                className="bg-primary/10 text-primary hover:bg-primary/15 border-none px-1.5 py-0.5 text-xs font-black"
              >
                {t("settings.mcp_server_item.tools_badge", {
                  count: isRunning?.tools_count ?? 0,
                })}
              </Badge>
            )}
            {isRunning?.subscription?.stale && (
              <Badge
                variant="outline"
                className="border-status-warning/40 bg-status-warning/10 text-status-warning px-1.5 py-0.5 text-xs font-black"
              >
                {t("settings.mcp_server_item.stale_badge")}
              </Badge>
            )}
            <span
              className={cn(
                "h-1.5 w-1.5 shrink-0 rounded-full shadow-sm",
                s.dot,
              )}
            />
            <span className={cn("text-xs font-bold whitespace-nowrap", s.text)}>
              {t(`settings.mcp_server_item.status_${displayStatus}`)}
            </span>
            {isEnabled && <McpIsolationBadge isolation={isRunning?.isolation} />}
            {isEnabled && isRunning?.isolation?.manual_start && (
              <Button
                variant="outline"
                size="sm"
                disabled={isRestarting}
                onClick={() => onRestart(serverKey)}
              >
                {t("settings.mcp_isolation.manual_start.button")}
              </Button>
            )}
          </div>
          <div className="text-muted-foreground mt-1 max-w-48 truncate font-mono text-xs opacity-60 sm:max-w-md">
            {/* remote 條目無本地指令，顯示端點；否則會渲染出 `undefined`（ADR-0194）。 */}
            {serverCfg.url
              ? serverCfg.url
              : `${serverCfg.command ?? ""} ${serverCfg.args?.join(" ") ?? ""}`}
          </div>
          {showError && (
            <div className="text-status-error mt-1 max-w-md text-xs font-bold break-words whitespace-pre-wrap">
              {t("settings.mcp_server_item.error_label")}:{" "}
              {errorText}
            </div>
          )}
          {isRunning?.cache?.enabled && (
            <div className="text-muted-foreground mt-1 text-xs font-bold">
              {t("settings.mcp_server_item.cache_status", {
                entries: isRunning.cache.entries,
                hits: isRunning.cache.hits,
              })}
            </div>
          )}
          {isOAuth && (
            <div className="mt-3 space-y-2">
              <McpOAuthStatus
                status={oauth.status}
                busy={oauth.busy}
                error={oauth.error}
                onConnect={() => void oauth.connect()}
                onDisconnect={() => void oauth.disconnect()}
              />
              {oauth.status === "selection_required" && (
                <McpOAuthSelection
                  authorizationServers={oauth.authorizationServers}
                  disabled={oauth.busy}
                  onSelect={(issuer) => void oauth.connect(issuer)}
                />
              )}
            </div>
          )}
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-2 opacity-0 transition-opacity group-hover:opacity-100">
        <Button
          variant="outline"
          size="sm"
          onClick={() => onToggle(serverKey)}
          className={cn(
            "h-7 rounded-lg px-3 text-xs font-bold transition-all",
            isEnabled
              ? "border-border text-muted-foreground hover:text-foreground"
              : "border-primary/20 bg-primary/10 text-primary hover:bg-primary/20",
          )}
        >
          {isEnabled
            ? t("settings.mcp_server_item.disable_button")
            : t("settings.mcp_server_item.enable_button")}
        </Button>
        <Button
          variant="ghost"
          size="icon"
          aria-label={t("settings.mcp_server_item.restart_aria", { serverKey })}
          onClick={() => onRestart(serverKey)}
          disabled={!isEnabled || isRestarting}
          className="text-muted-foreground hover:text-primary hover:bg-primary/5 h-8 w-8 rounded-lg disabled:opacity-40"
        >
          <RotateCw className={cn("h-4 w-4", isRestarting && "animate-spin")} />
        </Button>
        <Button
          variant="ghost"
          size="icon"
          aria-label={t("settings.mcp_server_item.tool_policy_aria", {
            serverKey,
          })}
          onClick={() => onManageTools(serverKey)}
          className="text-muted-foreground hover:text-primary hover:bg-primary/5 h-8 w-8 rounded-lg"
        >
          <ListChecks className="h-4 w-4" />
        </Button>
        <Button
          variant="ghost"
          size="icon"
          aria-label={t("settings.mcp_server_item.edit_aria", { serverKey })}
          onClick={() => onEdit(serverKey)}
          className="text-muted-foreground hover:text-primary hover:bg-primary/5 h-8 w-8 rounded-lg"
        >
          <Settings2 className="h-4 w-4" />
        </Button>
        <Button
          variant="ghost"
          size="icon"
          aria-label={t("settings.mcp_server_item.delete_aria", { serverKey })}
          onClick={() => onDelete(serverKey)}
          className="text-muted-foreground hover:text-destructive hover:bg-destructive/10 h-8 w-8 rounded-lg"
        >
          <X className="h-4 w-4" />
        </Button>
      </div>
    </div>
  );
}
