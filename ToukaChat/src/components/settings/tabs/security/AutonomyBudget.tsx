import { useTranslation } from "react-i18next";
import { Zap, RefreshCw } from "lucide-react";
import { AutonomyConfig } from "@/lib/types";
import { Card, CardContent } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Slider } from "@/components/ui/slider";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Input } from "@/components/ui/input";

interface Props {
  autonomyConfig: AutonomyConfig | null;
  setAutonomyConfig: (a: AutonomyConfig | null) => void;
  isSavingAutonomy: boolean;
  saveAutonomy: (a: AutonomyConfig) => Promise<boolean>;
}

export function AutonomyBudget({
  autonomyConfig,
  setAutonomyConfig,
  isSavingAutonomy,
  saveAutonomy
}: Props) {
  const { t } = useTranslation();
  if (!autonomyConfig) {
    return (
      <div className="p-12 text-center border-2 border-dashed border-border rounded-3xl text-sm text-muted-foreground italic">
        {t('settings.autonomy_budget.loading')}
      </div>
    );
  }

  return (
    <section className="space-y-6 pt-12 border-t border-border">
      <div className="flex items-center justify-between">
        <div className="space-y-1">
          <h4 className="text-sm font-bold flex items-center gap-2 text-foreground">
            <Zap className="w-5 h-5 text-primary" /> {t('settings.autonomy_budget.title')}
          </h4>
          <p className="text-xs text-muted-foreground">{t('settings.autonomy_budget.description')}</p>
        </div>
        <Button
          onClick={async () => {
            if (!autonomyConfig) return;
            await saveAutonomy(autonomyConfig);
          }}
          disabled={isSavingAutonomy || !autonomyConfig}
          className="bg-primary-button text-white rounded-xl text-xs font-bold hover:opacity-90 transition-all shadow-lg shadow-primary/20"
        >
          {isSavingAutonomy ? <RefreshCw className="w-3 h-3 animate-spin" /> : t('settings.autonomy_budget.save_button')}
        </Button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <Card className="bg-muted/20 rounded-3xl border border-border">
          <CardContent className="pt-6 space-y-4">
            <div className="flex items-center justify-between">
              <Label className="text-xs font-bold text-foreground">{t('settings.autonomy_budget.max_tool_rounds_label')}</Label>
              <span className="text-xs font-mono text-primary font-bold">
                {(autonomyConfig.react_loop?.max_tool_rounds ?? 0) === 0
                  ? t('settings.autonomy_budget.unlimited')
                  : autonomyConfig.react_loop?.max_tool_rounds}
              </span>
            </div>
            {/* ADR-0146：工具輪保險絲（0＝不限）。互動預設不限；無人心跳一律有限
                （排程值＞本保險絲＞後端保底 50）。舊 nudge 旋鈕（default_max_slots/
                root max_react_slots 雙寫）已隨機制移除。 */}
            <Slider
              min={0}
              max={500}
              step={10}
              value={[autonomyConfig.react_loop?.max_tool_rounds ?? 0]}
              onValueChange={val => {
                setAutonomyConfig({
                  ...autonomyConfig,
                  react_loop: { max_tool_rounds: val[0] }
                });
              }}
              className="accent-primary"
            />
            <p className="text-xs text-muted-foreground">{t('settings.autonomy_budget.max_tool_rounds_desc')}</p>
          </CardContent>
        </Card>

        <Card className="bg-muted/20 rounded-3xl border border-border">
          <CardContent className="pt-6 space-y-4">
            <div className="flex items-center justify-between">
              <Label className="text-xs font-bold text-foreground">{t('settings.autonomy_budget.max_repair_attempts_label')}</Label>
              <span className="text-xs font-mono text-primary font-bold">
                {autonomyConfig.self_repair?.max_attempts ?? 5}
              </span>
            </div>
            <Slider
              min={0}
              max={10}
              step={1}
              value={[autonomyConfig.self_repair?.max_attempts ?? 5]}
              onValueChange={val => {
                setAutonomyConfig({
                  ...autonomyConfig,
                  self_repair: autonomyConfig.self_repair ? {
                    ...autonomyConfig.self_repair,
                    max_attempts: val[0]
                  } : {
                    enabled: true,
                    max_attempts: val[0],
                    repair_prompt: ""
                  }
                });
              }}
              className="accent-primary"
            />
          </CardContent>
        </Card>
      </div>

      <Card className="bg-muted/20 rounded-3xl border border-border">
        <CardContent className="pt-6 space-y-4">
          <div className="flex items-center justify-between gap-4">
            <div className="space-y-1">
              <Label className="text-xs font-bold text-foreground">
                {t('settings.autonomy_budget.cost_cap_label')}
              </Label>
              <p className="text-xs text-muted-foreground">
                {t('settings.autonomy_budget.cost_cap_desc')}
              </p>
            </div>
            <Switch
              aria-label={t('settings.autonomy_budget.cost_cap_enable_label')}
              checked={autonomyConfig.cap?.enabled ?? false}
              onCheckedChange={(checked) => {
                setAutonomyConfig({
                  ...autonomyConfig,
                  cap: {
                    enabled: checked,
                    max_total_cost_usd: autonomyConfig.cap?.max_total_cost_usd ?? null,
                  },
                });
              }}
            />
          </div>
          {autonomyConfig.cap?.enabled && (
            <div className="flex items-center justify-between gap-4">
              <Label className="text-xs font-bold text-foreground">
                {t('settings.autonomy_budget.cost_cap_limit_label')}
              </Label>
              <Input
                type="number"
                min={0}
                step="0.01"
                value={autonomyConfig.cap?.max_total_cost_usd ?? ''}
                onChange={(e) => {
                  const raw = e.target.value;
                  setAutonomyConfig({
                    ...autonomyConfig,
                    cap: {
                      enabled: true,
                      max_total_cost_usd: raw === '' ? null : Number(raw),
                    },
                  });
                }}
                className="w-32 rounded-xl"
              />
            </div>
          )}
        </CardContent>
      </Card>
    </section>
  );
}
