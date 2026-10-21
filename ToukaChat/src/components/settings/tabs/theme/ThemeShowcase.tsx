// src/components/settings/tabs/theme/ThemeShowcase.tsx
"use client";

import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Card, CardHeader, CardTitle, CardContent, CardFooter } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import {
  Trash, Search, Settings, Activity, Loader2, User, Type
} from "lucide-react";
import { cn } from "@/lib/utils";

interface AgentIDPreviewProps {
  name: string;
}

function AgentIDPreview({ name }: AgentIDPreviewProps) {
  const { t } = useTranslation();
  return (
    <div className="p-4 rounded-xl border border-border bg-muted/20 relative pt-8 group transition-all hover:border-primary/30 shadow-sm">
      <div 
        className="absolute top-2 left-4 px-2 py-0.5 text-xs font-black uppercase tracking-widest rounded-full border"
        style={{ 
          color: `var(--color-agent-${name.toLowerCase()}, var(--primary))`,
          backgroundColor: `var(--color-agent-${name.toLowerCase()}-accent, color-mix(in srgb, var(--primary) 10%, transparent))`,
          borderColor: `var(--color-agent-${name.toLowerCase()}, var(--primary))`,
          opacity: 0.8
        }}
      >
        {name}
      </div>
      <div className="text-xs text-muted-foreground leading-relaxed">
        {t("settings.theme_showcase.agent_sample_reply", { name })}
      </div>
    </div>
  );
}

export function ThemeShowcase({ className }: { className?: string }) {
  const { t } = useTranslation();
  return (
    <div className={cn("space-y-8 animate-in fade-in duration-500", className)}>
      {/* 1. Agent Identities (New Inclusion) */}
      <section className="space-y-4">
        <h4 className="text-xs font-black uppercase tracking-widest text-muted-foreground flex items-center gap-2">
            <User className="w-3.5 h-3.5" /> {t("settings.theme_showcase.section_agents")}
        </h4>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
           <AgentIDPreview name="AgentAlpha" />
           <AgentIDPreview name="AgentBeta" />
           <AgentIDPreview name="DebugAgent" />
        </div>
      </section>

      {/* Font-size live preview (P2)：文字綁定 --chat-font-size，拉動字級滑桿即時可見。 */}
      <section className="space-y-4">
        <h4 className="text-xs font-black uppercase tracking-widest text-muted-foreground flex items-center gap-2">
            <Type className="w-3.5 h-3.5" /> {t("settings.theme_showcase.font_preview_heading")}
        </h4>
        <div className="rounded-2xl border border-border bg-muted/40 p-4">
          <p className="text-foreground leading-relaxed" style={{ fontSize: 'var(--chat-font-size)' }}>
            {t("settings.theme_showcase.font_preview_sample")}
          </p>
        </div>
      </section>

      {/* 2. Core UI Palette */}
      <section className="space-y-4">
        <h4 className="text-xs font-black uppercase tracking-widest text-muted-foreground flex items-center gap-2">
            <Activity className="w-3.5 h-3.5" /> {t("settings.theme_showcase.section_components")}
        </h4>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {/* Buttons */}
          <Card className="bg-card/50">
            <CardHeader className="py-4">
              <CardTitle className="text-sm">{t("settings.theme_showcase.card_buttons")}</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="flex flex-wrap gap-2">
                <Button size="sm">{t("settings.theme_showcase.btn_primary")}</Button>
                <Button size="sm" variant="secondary">{t("settings.theme_showcase.btn_secondary")}</Button>
                <Button size="sm" variant="outline">{t("settings.theme_showcase.btn_outline")}</Button>
                <Button size="sm" variant="ghost">{t("settings.theme_showcase.btn_ghost")}</Button>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button size="sm" variant="destructive"><Trash className="w-3.5 h-3.5 mr-1" /> {t("settings.theme_showcase.btn_delete")}</Button>
                <Button size="sm" disabled><Loader2 className="w-3.5 h-3.5 mr-1 animate-spin" /> {t("settings.theme_showcase.btn_busy")}</Button>
                <Button size="icon" variant="outline" aria-label={t("settings.theme_showcase.settings_aria")} className="h-8 w-8"><Settings className="w-3.5 h-3.5" /></Button>
              </div>
            </CardContent>
          </Card>

          {/* Form & Toggles */}
          <Card className="bg-card/50">
            <CardHeader className="py-4">
              <CardTitle className="text-sm">{t("settings.theme_showcase.card_inputs")}</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="relative">
                <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground" />
                <Input placeholder={t("settings.theme_showcase.search_placeholder")} className="pl-8 h-9 text-xs" />
              </div>
              <div className="flex items-center justify-between p-2 rounded-lg bg-muted/40 border">
                <span className="text-xs font-bold">{t("settings.theme_showcase.enabled_features")}</span>
                <Switch checked />
              </div>
            </CardContent>
          </Card>
        </div>
      </section>

      {/* 3. Complex Card Example */}
      <section className="space-y-4">
        <Card className="border-primary/20 bg-primary/5 overflow-hidden">
          <CardHeader className="py-4">
            <CardTitle className="text-sm flex items-center gap-2">
              <Activity className="w-4 h-4 text-primary" /> {t("settings.theme_showcase.card_metrics")}
            </CardTitle>
          </CardHeader>
          <CardContent className="pb-4">
            <div className="text-2xl font-black text-primary">{t("settings.theme_showcase.metric_active")}</div>
            <div className="mt-1 text-xs text-muted-foreground uppercase tracking-tighter">
              {t("settings.theme_showcase.metric_desc")}
            </div>
          </CardContent>
          <CardFooter className="bg-muted py-2 border-t text-xs font-mono opacity-60">
            {t("settings.theme_showcase.metric_footer")}
          </CardFooter>
        </Card>
      </section>
    </div>
  );
}
