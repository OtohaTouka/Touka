import type { Metadata } from "next";

// WCAG 2.4.2：client page 無法自帶 metadata，由 segment layout 提供獨立頁面標題
export const metadata: Metadata = { title: "連線管理 — Touka Console" };

export default function ConnectionsLayout({ children }: { children: React.ReactNode }) {
  return children;
}
