import { useState, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { PlusCircle, ShieldAlert } from "lucide-react";
import { toast } from "sonner";
import { useConfirmToast } from "@/hooks/useConfirmToast";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { InstalledMcpConfig } from "@/lib/mcp_version";
import { useDirtyGuard } from "@/components/settings/SettingsDirtyContext";

interface Props {
  mcpConfig: InstalledMcpConfig;
  editingMcpKey: string | null;
  onSave: (config: InstalledMcpConfig) => Promise<boolean>;
  onCancel: () => void;
}

type TransportMode = "stdio" | "remote";

export function McpServerForm({ mcpConfig, editingMcpKey, onSave, onCancel }: Props) {
  const { t } = useTranslation();
  const { confirmToast } = useConfirmToast();
  const [newMcpName, setNewMcpName] = useState("");
  const [newMcpCommand, setNewMcpCommand] = useState("");
  const [newMcpArgs, setNewMcpArgs] = useState("");
  const [newMcpEnv, setNewMcpEnv] = useState("");
  // remote 形狀（ADR-0194）：url＋headers；與 command/args 互斥。
  const [transport, setTransport] = useState<TransportMode>("stdio");
  const [newMcpUrl, setNewMcpUrl] = useState("");
  const [newMcpHeaders, setNewMcpHeaders] = useState("");
  const [isSaving, setIsSaving] = useState(false);

  const isRemote = transport === "remote";

  // C5：未存草稿守衛——編輯態比對原值；新增態任一欄位非空即 dirty。
  const _editCfg = editingMcpKey ? mcpConfig[editingMcpKey] : undefined;
  const isDirty = editingMcpKey
    ? (newMcpName !== editingMcpKey
        || newMcpCommand !== (_editCfg?.command ?? "")
        || newMcpArgs !== (_editCfg?.args ? _editCfg.args.join(", ") : "")
        || newMcpUrl !== (_editCfg?.url ?? "")
        || newMcpHeaders !== (_editCfg?.headers ? JSON.stringify(_editCfg.headers) : "")
        || newMcpEnv !== (_editCfg?.env ? JSON.stringify(_editCfg.env) : ""))
    : !!(newMcpName || newMcpCommand || newMcpArgs || newMcpEnv || newMcpUrl || newMcpHeaders);
  useDirtyGuard("mcp-server-form", isDirty);

  useEffect(() => {
    if (editingMcpKey && mcpConfig[editingMcpKey]) {
      const cfg = mcpConfig[editingMcpKey];
      // eslint-disable-next-line react-hooks/set-state-in-effect -- 依 editingMcpKey 填入編輯態表單欄位
      setNewMcpName(editingMcpKey);
      // 依既有條目的形狀決定模式——remote 條目若被當成空 command 的 stdio 載入，
      // 使用者會連補憑證都做不到（存檔鈕永遠 disabled），且存檔會抹掉 url/headers。
      setTransport(cfg.url ? "remote" : "stdio");
      setNewMcpCommand(cfg.command ?? "");
      setNewMcpArgs(cfg.args ? cfg.args.join(", ") : "");
      setNewMcpUrl(cfg.url ?? "");
      setNewMcpHeaders(cfg.headers ? JSON.stringify(cfg.headers) : "");
      setNewMcpEnv(cfg.env ? JSON.stringify(cfg.env) : "");
    } else {
      setNewMcpName("");
      setTransport("stdio");
      setNewMcpCommand("");
      setNewMcpArgs("");
      setNewMcpUrl("");
      setNewMcpHeaders("");
      setNewMcpEnv("");
    }
  }, [editingMcpKey, mcpConfig]);

  const handleSaveLocal = () => {
    if (!newMcpName || (isRemote ? !newMcpUrl : !newMcpCommand)) return;

    let parsedEnv = undefined;
    if (newMcpEnv) {
        try {
            parsedEnv = JSON.parse(newMcpEnv);
        } catch {
            // P2：無效 env JSON 改走 sonner（主題感知、非阻塞），保留具體錯誤訊息。
            toast.error(t("settings.mcp_server_form.errors.invalid_env_json"));
            return;
        }
    }

    let parsedHeaders = undefined;
    if (isRemote && newMcpHeaders) {
        try {
            parsedHeaders = JSON.parse(newMcpHeaders);
        } catch {
            toast.error(t("settings.mcp_server_form.errors.invalid_headers_json"));
            return;
        }
    }

    const updatedConfig = { ...mcpConfig };
    const prev = editingMcpKey ? mcpConfig[editingMcpKey] : undefined;
    // OCP：保留所有非傳輸欄位（含未來新增的認證／能力／安全政策），只重建使用者
    // 在本表單實際編輯的 transport 欄位。逐欄白名單會讓新增政策在例行編輯時被抹除。
    const {
      command: _previousCommand,
      args: _previousArgs,
      url: _previousUrl,
      headers: _previousHeaders,
      env: _previousEnv,
      ...preserved
    } = prev ?? {};
    const enabled = prev?.enabled ?? true;
    updatedConfig[newMcpName] = isRemote
      ? { ...preserved, url: newMcpUrl, headers: parsedHeaders ?? {}, env: parsedEnv, enabled }
      : {
          ...preserved,
          command: newMcpCommand,
          args: newMcpArgs ? newMcpArgs.split(",").map(a => a.trim()) : [],
          env: parsedEnv,
          enabled,
        };

    if (editingMcpKey && editingMcpKey !== newMcpName) {
        delete updatedConfig[editingMcpKey];
    }

    // P2：存檔前二次確認——此指令會在伺服器執行（縱深防禦，與 Hub 安裝路徑的警告對齊）。
    confirmToast(t("settings.mcp_server_form.confirm_manual_add"), async () => {
        setIsSaving(true);
        const success = await onSave(updatedConfig);
        if (success && !editingMcpKey) {
            setNewMcpName("");
            setNewMcpCommand("");
            setNewMcpArgs("");
            setNewMcpUrl("");
            setNewMcpHeaders("");
            setNewMcpEnv("");
        }
        setIsSaving(false);
    });
  };

  return (
    <Card className="bg-muted/30 rounded-3xl border border-border space-y-4 shadow-sm">
      <CardHeader className="pb-2">
        <CardTitle className="text-xs font-black uppercase text-muted-foreground tracking-widest flex items-center gap-2">
          <PlusCircle size={14} className="text-primary" /> 
          {editingMcpKey ? t("settings.mcp_server_form.editing_title", { name: editingMcpKey }) : t("settings.mcp_server_form.add_title")}
        </CardTitle>
      </CardHeader>
      
      <CardContent className="space-y-4 pt-2">
        {/* P2 信任警告：手動新增＝設定一個會在伺服器執行的指令（與 Hub 安裝路徑對齊）。 */}
        <div className="flex items-start gap-2.5 rounded-xl border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive">
          <ShieldAlert className="w-4 h-4 shrink-0 mt-0.5" />
          <span className="leading-relaxed">
            {t(isRemote ? "settings.mcp_server_form.security_warning_remote" : "settings.mcp_server_form.security_warning")}
          </span>
        </div>

        {/* 傳輸形態切換（ADR-0194）：本地 stdio 與遠端 streamable HTTP 互斥。 */}
        <div className="flex items-center gap-2">
          {(["stdio", "remote"] as const).map(mode => (
            <Button
              key={mode}
              type="button"
              variant={transport === mode ? "default" : "outline"}
              size="sm"
              onClick={() => setTransport(mode)}
              className="rounded-xl text-xs font-bold"
            >
              {t(`settings.mcp_server_form.transport_${mode}`)}
            </Button>
          ))}
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div className="space-y-1.5">
            <Label className="ml-1 text-xs font-bold uppercase tracking-tighter text-muted-foreground">{t("settings.mcp_server_form.server_name_label")}</Label>
            <Input
              placeholder={t("settings.mcp_server_form.server_name_ph")}
              value={newMcpName}
              onChange={e => setNewMcpName(e.target.value)}
              className="bg-background border border-border rounded-xl px-4 py-2.5 text-sm h-11"
            />
          </div>
          <div className="space-y-1.5">
            <Label className="ml-1 text-xs font-bold uppercase tracking-tighter text-muted-foreground">
              {t(isRemote ? "settings.mcp_server_form.url_label" : "settings.mcp_server_form.command_label")}
            </Label>
            <Input
              placeholder={t(isRemote ? "settings.mcp_server_form.url_ph" : "settings.mcp_server_form.command_ph")}
              value={isRemote ? newMcpUrl : newMcpCommand}
              onChange={e => (isRemote ? setNewMcpUrl(e.target.value) : setNewMcpCommand(e.target.value))}
              className="bg-background border border-border rounded-xl px-4 py-2.5 text-sm h-11"
            />
          </div>
        </div>

        <div className="space-y-1.5">
          <Label className="ml-1 text-xs font-bold uppercase tracking-tighter text-muted-foreground">
            {t(isRemote ? "settings.mcp_server_form.headers_label" : "settings.mcp_server_form.args_label")}
          </Label>
          <Input
            placeholder={t(isRemote ? "settings.mcp_server_form.headers_ph" : "settings.mcp_server_form.args_ph")}
            value={isRemote ? newMcpHeaders : newMcpArgs}
            onChange={e => (isRemote ? setNewMcpHeaders(e.target.value) : setNewMcpArgs(e.target.value))}
            className="bg-background border border-border rounded-xl px-4 py-2.5 text-sm font-mono h-11"
          />
        </div>

        <div className="space-y-1.5">
          <Label className="ml-1 text-xs font-bold uppercase tracking-tighter text-muted-foreground">{t("settings.mcp_server_form.env_label")}</Label>
          <Input
            placeholder={t("settings.mcp_server_form.env_ph")}
            value={newMcpEnv} 
            onChange={e => setNewMcpEnv(e.target.value)} 
            className="bg-background border border-border rounded-xl px-4 py-2.5 text-xs font-mono h-11"
          />
        </div>

        <div className="flex items-center gap-3 pt-2">
          <Button 
            onClick={handleSaveLocal}
            disabled={!newMcpName || (isRemote ? !newMcpUrl : !newMcpCommand) || isSaving}
            className="flex-1 py-6 bg-primary-button text-white rounded-xl text-sm font-bold hover:opacity-90 shadow-lg shadow-primary/20 transition-all flex items-center justify-center gap-2"
          >
            {editingMcpKey ? t("settings.mcp_server_form.finish_edit_button") : t("settings.mcp_server_form.add_button")}
          </Button>
          {editingMcpKey && (
            <Button 
              variant="outline"
              onClick={onCancel} 
              className="px-6 py-6 bg-background border-border text-muted-foreground rounded-xl text-sm font-bold hover:bg-accent transition-all"
            >
              {t("settings.mcp_server_form.cancel_button")}
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
