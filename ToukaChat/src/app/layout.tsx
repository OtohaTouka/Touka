import type { Metadata } from "next";
import { I18nProvider } from "@/lib/i18n/I18nProvider";
import { SystemStatusProvider } from "@/lib/SystemStatusContext";
import { ThemeProvider } from "@/lib/ThemeContext";
import { ThemeProvider as NextThemesProvider } from "next-themes";
import { SessionProvider } from "@/lib/SessionContext";
import { Toaster } from "@/components/ui/sonner";
import { AuthProvider } from "@/contexts/AuthContext";
import { AuthGuard } from "@/components/AuthGuard";
import { SetupNoticesProvider } from "@/components/setup/SetupNoticesProvider";
import { RetiredSnsStorageCleanup } from "@/components/security/RetiredSnsStorageCleanup";
import "./globals.css";

export const metadata: Metadata = {
  title: "Touka Console",
  description: "Advanced Cross-Modal AI Agent Proxy & Memory System",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-TW" suppressHydrationWarning>
      <head>
        <link href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:wght,FILL@100..700,0..1&display=swap" rel="stylesheet" />
      </head>
      <body className="bg-background text-foreground antialiased font-display flex h-screen overflow-hidden">
        {/* WCAG 2.4.1：skip link——鍵盤聚焦時浮現，跳過側欄直達主內容。
            樣式定義於 globals.css 的 .skip-link（Tailwind v4 下 focus:not-sr-only 失效） */}
        <a href="#main-content" className="skip-link">
          跳至主內容
        </a>
        <AuthProvider>
          <I18nProvider>
            <RetiredSnsStorageCleanup />
            {/* mode 軸（亮/暗/跟隨系統）由 next-themes 管理：attribute="class" 切換 <html> 的
                .light/.dark；enableSystem 接 prefers-color-scheme；FOUC 由其 pre-paint 腳本處理。
                自製 ThemeProvider（accent 軸）置於其內，訂閱 resolvedTheme 注入對應中性色（ADR-0105）。 */}
            <NextThemesProvider attribute="class" enableSystem defaultTheme="dark" storageKey="touka_theme_mode">
              <ThemeProvider>
                <SystemStatusProvider>
                  <AuthGuard>
                    <SessionProvider>
                      {/* 首次設定警告畫在左側邊欄裡；這裡只放狀態與精靈，不能放會佔版面的元素（body 是左右排列） */}
                      <SetupNoticesProvider>
                        {children}
                        <Toaster position="bottom-right" />
                      </SetupNoticesProvider>
                    </SessionProvider>
                  </AuthGuard>
                </SystemStatusProvider>
              </ThemeProvider>
            </NextThemesProvider>
          </I18nProvider>
        </AuthProvider>
      </body>
    </html>
  );
}
