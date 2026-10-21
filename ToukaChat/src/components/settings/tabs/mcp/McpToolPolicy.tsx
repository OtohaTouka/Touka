import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { ListChecks, Loader2 } from "lucide-react";
import { InstalledMcpConfig, McpServerToolsView } from "@/lib/mcp_version";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Button } from "@/components/ui/button";

export interface McpToolPolicyProps {
  serverKey: string;
  serverCfg: InstalledMcpConfig[string];
  fetchServerTools: (key: string) => Promise<McpServerToolsView | null>;
  onSave: (enabledTools: string[]) => void;
  onClose: () => void;
}

/**
 * per-installation 工具治理面板（ADR-0195 軸 A）。
 *
 * 語意提醒（三態，錯了會靜默）：
 * - `enabled_tools === undefined/null` ＝未設＝全開。
 * - `["*"]` ＝明示全開。**「全選」必須送這個**——送 `null` 會被後端的
 *   `_preserve_masked_env` 當成「客戶端沒回傳」而自磁碟還原成舊白名單。
 * - `[]` ＝明示全關。
 *
 * 此政策對**所有身分**生效，Owner 也不例外——它不是權限邊界，是 owner 對自己 agent 的
 * 自我約束（RBAC 的身分面收斂另在安全分頁）。
 */
export function McpToolPolicy({
  serverKey,
  serverCfg,
  fetchServerTools,
  onSave,
  onClose,
}: McpToolPolicyProps) {
  const { t } = useTranslation();
  const [view, setView] = useState<McpServerToolsView | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());

  useEffect(() => {
    let alive = true;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- 換伺服器時重置載入態；抓取本身即副作用
    setLoading(true);
    setFailed(false);
    fetchServerTools(serverKey).then((data) => {
      if (!alive) return;
      if (!data) {
        setFailed(true);
      } else {
        setView(data);
        // 初始勾選狀態直接取後端算好的 `enabled`（含 disabled_tools 的 deny-wins），
        // 不在前端重刻比對規則——SSOT 在 `mcp_tool_policy.py`。
        setSelected(
          new Set(
            (data.tools ?? []).filter((x) => x.enabled).map((x) => x.name),
          ),
        );
      }
      setLoading(false);
    });
    return () => {
      alive = false;
    };
  }, [serverKey, fetchServerTools]);

  const toggle = (name: string, checked: boolean) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (checked) next.add(name);
      else next.delete(name);
      return next;
    });
  };

  const handleSave = () => {
    if (!view) return;
    const all = (view.tools ?? []).map((x) => x.name);
    // 全選時送 `["*"]` 而非逐項列舉：上游日後新增工具會自動開放，語意才等同「不設限」。
    const allSelected = all.length > 0 && all.every((n) => selected.has(n));
    onSave(allSelected ? ["*"] : all.filter((n) => selected.has(n)));
  };

  const isDisabledByDenylist = (name: string) =>
    (view?.disabled_tools ?? []).includes(name) ||
    (view?.disabled_tools ?? []).includes("*");

  return (
    <Card className="bg-muted/30 border-border space-y-4 rounded-3xl border shadow-sm">
      <CardHeader className="pb-2">
        <CardTitle className="text-muted-foreground flex items-center gap-2 text-xs font-black tracking-widest uppercase">
          <ListChecks size={14} className="text-primary" />
          {t("settings.mcp_tool_policy.title", { name: serverKey })}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4 pt-2">
        <p className="text-muted-foreground text-xs leading-relaxed">
          {t("settings.mcp_tool_policy.description")}
        </p>

        {loading && (
          <div className="text-muted-foreground flex items-center gap-2 p-3 text-xs">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            {t("settings.mcp_tool_policy.loading")}
          </div>
        )}

        {!loading && failed && (
          <p className="border-destructive/30 bg-destructive/10 text-destructive rounded-xl border p-3 text-xs">
            {t("settings.mcp_tool_policy.fetch_failed")}
          </p>
        )}

        {!loading && !failed && (view?.tools?.length ?? 0) === 0 && (
          <p className="border-border bg-background/50 text-muted-foreground rounded-xl border p-3 text-xs">
            {serverCfg.enabled === false
              ? t("settings.mcp_tool_policy.server_disabled")
              : t("settings.mcp_tool_policy.not_connected")}
          </p>
        )}

        {!loading && !failed && (view?.tools?.length ?? 0) > 0 && (
          <div className="custom-scrollbar border-border bg-background/50 max-h-60 space-y-1 overflow-y-auto rounded-2xl border p-3">
            {view?.tools?.map((tool) => (
              <label
                key={tool.name}
                className="hover:bg-accent/50 group/tool flex cursor-pointer items-start gap-2 rounded-lg p-2 transition-colors"
              >
                <Checkbox
                  className="mt-0.5"
                  checked={selected.has(tool.name)}
                  disabled={isDisabledByDenylist(tool.name)}
                  onCheckedChange={(checked) => toggle(tool.name, !!checked)}
                />
                <span className="min-w-0">
                  <span className="text-foreground group-hover/tool:text-primary block font-mono text-xs font-bold transition-colors">
                    {tool.name}
                  </span>
                  {tool.description && (
                    <span className="text-muted-foreground mt-0.5 block text-xs leading-relaxed opacity-70">
                      {tool.description}
                    </span>
                  )}
                </span>
              </label>
            ))}
          </div>
        )}

        {(view?.disabled_tools?.length ?? 0) > 0 && (
          <p className="text-muted-foreground text-xs opacity-70">
            {t("settings.mcp_tool_policy.denylist_note", {
              tools: (view?.disabled_tools ?? []).join(", "),
            })}
          </p>
        )}

        <div className="flex justify-end gap-2">
          <Button variant="ghost" size="sm" onClick={onClose}>
            {t("settings.mcp_tool_policy.cancel")}
          </Button>
          <Button
            size="sm"
            onClick={handleSave}
            disabled={loading || failed || (view?.tools?.length ?? 0) === 0}
          >
            {t("settings.mcp_tool_policy.save")}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
