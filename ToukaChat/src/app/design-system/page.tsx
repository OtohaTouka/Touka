// src/app/design-system/page.tsx
"use client";

import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Moon, Sun, LayoutDashboard } from "lucide-react";
import { ThemeShowcase } from "@/components/settings/tabs/theme/ThemeShowcase";

/**
 * Design System Standalone Page
 * 現在重構為封裝 ThemeShowcase 組件，保持開發測試的便利性。
 */
export default function DesignSystemPage() {
  const { t } = useTranslation();
  const [isDarkMode, setIsDarkMode] = useState(false);

  return (
    <div className={`min-h-screen bg-background text-foreground p-8 pb-32 ${isDarkMode ? "dark" : ""}`}>
      <div className="max-w-6xl mx-auto space-y-12">
        
        {/* Header */}
        <header className="flex justify-between items-center border-b pb-8">
          <div>
            <h1 className="text-4xl font-black tracking-tight mb-2">Touka Design System</h1>
            <p className="text-muted-foreground">{t('app.design_system_page.subtitle')}</p>
          </div>
          <div className="flex items-center gap-4">
             <Button variant="outline" onClick={() => window.history.back()}>{t('app.design_system_page.back')}</Button>
             <Button
               variant="outline"
               size="icon"
               aria-label={t('app.design_system_page.toggle_theme_aria')}
               onClick={() => setIsDarkMode(!isDarkMode)}
               className="rounded-full"
             >
               {isDarkMode ? <Sun size={20} /> : <Moon size={20} />}
             </Button>
          </div>
        </header>

        <section className="space-y-6">
          <h2 className="text-2xl font-bold flex items-center gap-2">
            <LayoutDashboard className="text-primary" /> Visual Playground
          </h2>
          <div className="p-10 border border-border rounded-3xl bg-sidebar/30 shadow-inner">
             <ThemeShowcase className="max-w-4xl mx-auto" />
          </div>
        </section>

      </div>
    </div>
  );
}
