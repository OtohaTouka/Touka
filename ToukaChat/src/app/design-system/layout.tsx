import type { Metadata } from "next";

// WCAG 2.4.2：client page 無法自帶 metadata，由 segment layout 提供獨立頁面標題
export const metadata: Metadata = { title: "Design System — Touka Console" };

export default function DesignSystemLayout({ children }: { children: React.ReactNode }) {
  return children;
}
