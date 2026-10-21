import { useTranslation } from "react-i18next";
import { ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useConfirmToast } from "@/hooks/useConfirmToast";
import { useMcpIsolation } from "@/hooks/settings/useMcpIsolation";
import { McpSandboxPathsEditor } from "./McpSandboxPathsEditor";

// 只有第三方與 scaffold 能有額外允許的資料夾（後端 GET／PUT 同一條規則）
const PATH_CLASSES: readonly string[] = ["third_party", "scaffold"];

interface Props {
  serverKey: string;
  /** 存檔後的條目；改變時重讀（程式改了，bound 跟著變）。 */
  revision: unknown;
}

/** 一台 MCP 的來源（只能往嚴：標成代理人建立）與額外允許的資料夾。 */
export function McpIsolationSection({ serverKey, revision }: Props) {
  const { t } = useTranslation();
  const { confirmToast } = useConfirmToast();
  const { view, saving, error, markAgent, saveSandboxPaths } = useMcpIsolation(
    serverKey,
    revision,
  );
  // 遠端 MCP 不是 Touka 啟動的程式，沒有隔離設定
  if (view?.class === "remote") return null;

  const handleMarkAgent = () =>
    confirmToast(
      t("settings.mcp_isolation.mark_agent_confirm", { name: serverKey }),
      () => void markAgent(),
      t("settings.mcp_isolation.mark_agent_button"),
    );

  return (
    <Card className="bg-muted/30 border-border space-y-2 rounded-3xl border shadow-sm">
      <CardHeader className="pb-2">
        <CardTitle className="text-muted-foreground flex items-center gap-2 text-xs font-black tracking-widest uppercase">
          <ShieldCheck size={14} className="text-primary" />
          {t("settings.mcp_isolation.title")}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 pt-2 text-xs">
        {view && (
          <>
            <p>
              {t("settings.mcp_isolation.origin_label")}
              {t(`settings.mcp_isolation.origin.${view.origin ?? "none"}`)}
            </p>
            <p>
              {t("settings.mcp_isolation.class_label")}
              {t(`settings.mcp_isolation.class.${view.class}`)}
            </p>
            {(view.record_state === "stale" ||
              view.record_state === "unreadable") && (
              <p className="text-status-warning">
                {t(`settings.mcp_isolation.record_${view.record_state}`)}
              </p>
            )}
            {view.can_mark_agent && (
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={saving}
                onClick={handleMarkAgent}
                className="rounded-xl text-xs font-bold"
              >
                {t("settings.mcp_isolation.mark_agent_button")}
              </Button>
            )}
            {PATH_CLASSES.includes(view.class) && (
              <McpSandboxPathsEditor
                // 讀到新的內容（存檔成功、條目改了）才換掉草稿；失敗時保留人打的內容
                key={JSON.stringify([view.bound, view.sandbox_paths])}
                saved={view.sandbox_paths}
                saving={saving}
                onSave={saveSandboxPaths}
              />
            )}
          </>
        )}
        {error && (
          <p role="alert" className="text-status-error font-bold">
            {error}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
