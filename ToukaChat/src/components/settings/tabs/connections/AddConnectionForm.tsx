import { useState } from "react";
import { useTranslation } from "react-i18next";
import { PlusCircle, Save } from "lucide-react";
import { Connection } from "@/lib/types";
import { PROVIDER_CATALOG, PROVIDER_IDS, resolvePrefillUrl, type ProviderKey } from "@/lib/providerCatalog";
import { type ConnectionDraft } from "@/lib/connectionEdit";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useDirtyGuard } from "@/components/settings/SettingsDirtyContext";
import { DirectConnectionNotice } from "./DirectConnectionNotice";

interface Props {
  onAdd: (conn: Omit<Connection, 'id'>) => void;
  /** 編輯中的連線（null/未傳＝新增模式）。安全：永不帶入既有 apiKey。 */
  editing?: Connection | null;
  /** 儲存編輯（changeKey=false 時保留既有金鑰，見 applyConnectionEdit）。 */
  onSave?: (id: string, draft: ConnectionDraft, changeKey: boolean) => void;
  /** 取消編輯（回到新增模式）。 */
  onCancelEdit?: () => void;
}

export function AddConnectionForm({ onAdd, editing, onSave, onCancelEdit }: Props) {
  const { t } = useTranslation();
  const isEditing = !!editing;
  // 由 editing 初始化欄位；父層以 key={editing.id} 重掛載本元件，故切換編輯目標時會帶入新值。
  // 刻意**不**從 editing 帶入 apiKey——既有金鑰永不回填表單（安全）。
  const [newConnName, setNewConnName] = useState(editing?.name ?? "");
  const [newConnUrl, setNewConnUrl] = useState(editing?.url ?? "");
  const [newConnApiKey, setNewConnApiKey] = useState("");
  const [newConnProvider, setNewConnProvider] = useState<string>(editing?.provider ?? "proxy");
  const [newConnProtocol, setNewConnProtocol] = useState<string>(editing?.protocol ?? "openai");
  // 編輯模式才用：是否更換金鑰。未勾＝保留既有（表單永不持有既有金鑰）。
  const [changeKey, setChangeKey] = useState(false);
  // P5-#4 請求逾時（以「秒」輸入、存 ms）。空＝不限。
  const [newConnTimeout, setNewConnTimeout] = useState(
    editing?.requestTimeoutMs ? String(editing.requestTimeoutMs / 1000) : "",
  );

  // C5：未存草稿守衛——編輯態比對原值（含換金鑰勾選）；新增態任一非預設欄位即 dirty。
  const isDirty = isEditing && editing
    ? (newConnName !== (editing.name ?? "")
        || newConnUrl !== (editing.url ?? "")
        || newConnProvider !== (editing.provider ?? "proxy")
        || newConnProtocol !== (editing.protocol ?? "openai")
        || newConnTimeout !== (editing.requestTimeoutMs ? String(editing.requestTimeoutMs / 1000) : "")
        || changeKey || !!newConnApiKey)
    : !!(newConnName || newConnUrl || newConnApiKey || newConnTimeout
        || newConnProvider !== "proxy" || newConnProtocol !== "openai");
  useDirtyGuard("connection-form", isDirty);

  const handleProviderChange = (next: string) => {
    setNewConnProvider(next);
    setNewConnUrl((prev) => resolvePrefillUrl(next, prev));
  };

  const handleSubmit = () => {
    if (!newConnUrl) return;
    // 秒 → ms；空白/非正數＝不限（undefined）。
    const sec = Number(newConnTimeout);
    const requestTimeoutMs =
      newConnTimeout.trim() !== "" && Number.isFinite(sec) && sec > 0 ? Math.round(sec * 1000) : undefined;
    if (isEditing && editing && onSave) {
      onSave(
        editing.id,
        {
          name: newConnName,
          url: newConnUrl,
          provider: newConnProvider,
          protocol: newConnProtocol,
          apiKey: newConnApiKey,
          requestTimeoutMs,
        },
        changeKey,
      );
      return;
    }
    onAdd({
      name: newConnName || (newConnProvider === 'proxy' ? "Touka Proxy" : "New Connection"),
      url: newConnUrl,
      apiKey: newConnApiKey,
      provider: newConnProvider,
      protocol: newConnProtocol,
      enabled: true,
      requestTimeoutMs,
    });
    setNewConnName("");
    setNewConnUrl("");
    setNewConnApiKey("");
    setNewConnProtocol("openai");
    setNewConnTimeout("");
  };

  return (
    <Card className="bg-muted/30 rounded-3xl border border-border shadow-none">
      <CardHeader className="pb-2">
        <CardTitle className="text-xs font-black uppercase text-muted-foreground tracking-widest">
            {isEditing && editing
              ? t('connections.edit.editing_badge', { name: editing.name })
              : t('settings.add_connection_form.title')}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {!isEditing && (
          <p className="text-xs text-muted-foreground leading-relaxed rounded-lg border border-border bg-muted/40 p-3">
            {t('settings.add_connection_form.system_key_guidance')}
          </p>
        )}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div className="space-y-1.5">
            <Label className="ml-1 text-xs font-bold text-muted-foreground">{t('settings.add_connection_form.name_label')}</Label>
            <Input
              placeholder={t('settings.add_connection_form.name_placeholder')}
              value={newConnName}
              onChange={e => setNewConnName(e.target.value)}
              className="bg-background border border-border rounded-xl px-4 py-2.5 text-sm h-11"
            />
          </div>
          <div className="space-y-1.5">
            <Label className="ml-1 text-xs font-bold text-muted-foreground">{t('settings.add_connection_form.provider_label')}</Label>
            <Select value={newConnProvider} onValueChange={handleProviderChange}>
              <SelectTrigger className="bg-background border border-border rounded-xl px-4 py-2.5 text-sm h-11 text-foreground">
                <SelectValue placeholder={t('settings.add_connection_form.provider_placeholder')} />
              </SelectTrigger>
              <SelectContent>
                {PROVIDER_IDS.map((id) => (
                  <SelectItem key={id} value={id}>
                    {PROVIDER_CATALOG[id].icon} {t(`${PROVIDER_CATALOG[id].i18nBase}.label`)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <div className="sm:col-span-2 space-y-1.5">
            <Label className="ml-1 text-xs font-bold text-muted-foreground">{t('settings.add_connection_form.endpoint_url_label')}</Label>
            <Input
              placeholder={PROVIDER_CATALOG[newConnProvider as ProviderKey]?.defaultBaseUrl ?? t('settings.add_connection_form.endpoint_url_placeholder')}
              value={newConnUrl}
              onChange={e => setNewConnUrl(e.target.value)}
              className="bg-background border border-border rounded-xl px-4 py-2.5 text-sm h-11"
            />
            {PROVIDER_CATALOG[newConnProvider as ProviderKey] && (
              <p className="text-xs text-muted-foreground italic pl-1 mt-1">
                {t(`${PROVIDER_CATALOG[newConnProvider as ProviderKey].i18nBase}.hint`)}{' '}
                <a
                  href={PROVIDER_CATALOG[newConnProvider as ProviderKey].docUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-primary not-italic font-medium hover:underline"
                >
                  {t('connections.providers.doc_link_label')}
                </a>
              </p>
            )}
            <DirectConnectionNotice provider={newConnProvider} />
          </div>
          <div className="space-y-1.5">
            <Label className="ml-1 text-xs font-bold text-muted-foreground">{t('settings.add_connection_form.protocol_label')}</Label>
            <Select value={newConnProtocol} onValueChange={setNewConnProtocol}>
              <SelectTrigger className="bg-background border border-border rounded-xl px-4 py-2.5 text-sm h-11 text-foreground">
                <SelectValue placeholder={t('settings.add_connection_form.protocol_placeholder')} />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="openai">OpenAI</SelectItem>
                <SelectItem value="anthropic">Anthropic</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>

        <div className="space-y-1.5">
          <Label className="ml-1 text-xs font-bold text-muted-foreground">{t('settings.add_connection_form.timeout_label')}</Label>
          <Input
            type="number"
            min={0}
            inputMode="numeric"
            placeholder={t('settings.add_connection_form.timeout_placeholder')}
            value={newConnTimeout}
            onChange={e => setNewConnTimeout(e.target.value)}
            className="bg-background border border-border rounded-xl px-4 py-2.5 text-sm h-11"
          />
          <p className="text-xs text-muted-foreground italic pl-1">{t('settings.add_connection_form.timeout_hint')}</p>
        </div>

        <div className="space-y-1.5">
          <Label className="ml-1 text-xs font-bold text-muted-foreground">{t('settings.add_connection_form.api_key_label')}</Label>
          {isEditing && (
            <label className="flex items-center gap-2 pl-1 text-xs font-medium text-muted-foreground cursor-pointer">
              <Checkbox
                checked={changeKey}
                onCheckedChange={(c) => setChangeKey(!!c)}
                className="w-4 h-4 rounded border-primary/20 data-[state=checked]:bg-primary data-[state=checked]:border-primary"
              />
              {t('connections.edit.change_key_label')}
            </label>
          )}
          {(!isEditing || changeKey) ? (
            <Input
              type="password"
              placeholder="sk-..."
              value={newConnApiKey}
              onChange={e => setNewConnApiKey(e.target.value)}
              className="bg-background border border-border rounded-xl px-4 py-2.5 text-sm h-11"
            />
          ) : (
            <p className="text-xs text-muted-foreground italic pl-1">{t('connections.edit.key_kept_hint')}</p>
          )}
        </div>

        <div className="flex gap-2 mt-2">
          <Button
            onClick={handleSubmit}
            className="flex-1 py-6 bg-primary-button text-white rounded-xl text-sm font-bold hover:opacity-90 shadow-lg shadow-primary/20 transition-all flex items-center justify-center gap-2"
          >
            {isEditing ? <Save size={16}/> : <PlusCircle size={16}/>}
            {isEditing ? t('connections.edit.save') : t('settings.add_connection_form.add_button')}
          </Button>
          {isEditing && (
            <Button
              variant="outline"
              onClick={onCancelEdit}
              className="py-6 px-6 rounded-xl text-sm font-bold"
            >
              {t('connections.edit.cancel')}
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
