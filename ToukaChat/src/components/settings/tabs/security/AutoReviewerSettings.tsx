import { useTranslation } from "react-i18next";
import { ShieldCheck, RefreshCw } from "lucide-react";
import type { AutonomyConfig } from "@/lib/types";
import { useAutoReviewSettings } from "@/hooks/settings/useAutoReviewSettings";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

// Radix Select 不收空字串；「未設定」以此值表示，選到時存回空白（後端的未設定）。
const UNSET = "__unset__";

interface Props {
  autonomyConfig: AutonomyConfig | null;
  setAutonomyConfig: (a: AutonomyConfig | null) => void;
  isSavingAutonomy: boolean;
  saveAutonomy: (a: AutonomyConfig) => Promise<boolean>;
}

/** Auto 審查員模型選單（ADR-0246 決定 5）：只列已設定金鑰的模型，預設空白。 */
export function AutoReviewerSettings({ autonomyConfig, setAutonomyConfig, isSavingAutonomy, saveAutonomy }: Props) {
  const { t } = useTranslation();
  const { models, loadFailed, selectedModel, editable, setReviewerModel, timeoutS, timeoutMaxS, timeoutValid, setReviewerTimeout } =
    useAutoReviewSettings(autonomyConfig, setAutonomyConfig);
  if (!autonomyConfig) return null;
  const missingKey = !!selectedModel && !!models && !models.some(m => m.id === selectedModel);

  return (
    <section className="space-y-6 pt-12 border-t border-border">
      <div className="flex items-center justify-between gap-4">
        <div className="space-y-1">
          <h4 className="text-sm font-bold flex items-center gap-2 text-foreground">
            <ShieldCheck className="w-5 h-5 text-primary" /> {t('settings.auto_reviewer.title')}
          </h4>
          <p className="text-xs text-muted-foreground">{t('settings.auto_reviewer.description')}</p>
        </div>
        <Button
          onClick={() => void saveAutonomy(autonomyConfig)}
          disabled={isSavingAutonomy || !editable || !timeoutValid}
          className="bg-primary-button text-white rounded-xl text-xs font-bold hover:opacity-90 transition-all shadow-lg shadow-primary/20"
        >
          {isSavingAutonomy ? <RefreshCw className="w-3 h-3 animate-spin" /> : t('settings.auto_reviewer.save_button')}
        </Button>
      </div>

      <Card className="bg-muted/20 rounded-3xl border border-border">
        <CardContent className="pt-6 space-y-4">
          <Label className="text-xs font-bold text-foreground">{t('settings.auto_reviewer.model_label')}</Label>
          <Select value={selectedModel || UNSET} onValueChange={v => setReviewerModel(v === UNSET ? '' : v)} disabled={!editable}>
            <SelectTrigger aria-label={t('settings.auto_reviewer.model_label')} className="w-full md:w-96 rounded-xl text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={UNSET}>{t('settings.auto_reviewer.unset_option')}</SelectItem>
              {(models ?? []).map(m => (
                <SelectItem key={m.id} value={m.id}>{`${m.name} (${m.provider})`}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          <p className="text-xs text-muted-foreground">{t('settings.auto_reviewer.suggestion')}</p>
          {missingKey && (
            <p className="text-xs text-status-warning">{t('settings.auto_reviewer.missing_key', { model: selectedModel })}</p>
          )}
          {models?.length === 0 && <p className="text-xs text-status-warning">{t('settings.auto_reviewer.no_models')}</p>}
          {loadFailed && <p role="alert" className="text-xs text-status-error">{t('settings.auto_reviewer.load_failed')}</p>}
          <Label htmlFor="auto-reviewer-timeout" className="text-xs font-bold text-foreground">{t('settings.auto_reviewer.timeout_label')}</Label>
          <Input
            id="auto-reviewer-timeout"
            type="number"
            min={1}
            max={timeoutMaxS}
            step={1}
            value={Number.isFinite(timeoutS) ? timeoutS : ''}
            onChange={e => setReviewerTimeout(e.target.value === '' ? Number.NaN : Number(e.target.value))}
            disabled={!editable}
            className="w-32 rounded-xl"
          />
          <p className="text-xs text-muted-foreground">{t('settings.auto_reviewer.timeout_help')}</p>
          {editable && !timeoutValid && <p className="text-xs text-status-error">{t('settings.auto_reviewer.timeout_invalid')}</p>}
          <ul className="list-disc pl-4 space-y-1 text-xs text-muted-foreground">
            <li>{t('settings.auto_reviewer.data_notice')}</li>
            <li>{t('settings.auto_reviewer.cost_notice')}</li>
            <li>{t('settings.auto_reviewer.unset_notice')}</li>
          </ul>
        </CardContent>
      </Card>
    </section>
  );
}
