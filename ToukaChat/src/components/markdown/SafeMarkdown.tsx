"use client";

import React from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { useTranslation } from "react-i18next";
import { ImageOff } from "lucide-react";
import { getActiveProxyUrl } from "@/lib/connections";
import { decideMarkdownImage } from "@/lib/markdownImagePolicy";

const REMARK_PLUGINS = [remarkGfm];

/**
 * 模型輸出的 Markdown 圖片（ADR-0294）：Touka 自己的來源照常顯示、不帶 Referer；
 * 外部圖片不自動載入，改成「外部圖片（未載入）＋網站名稱」，點了才在新分頁開。
 */
function MarkdownImage({ src, alt, title }: React.ComponentPropsWithoutRef<"img">) {
  const { t } = useTranslation();
  const decision = decideMarkdownImage(
    typeof src === "string" ? src : undefined,
    typeof window === "undefined" ? "" : window.location.origin,
    getActiveProxyUrl(),
  );
  if (decision.kind === "load") {
    return <img src={decision.src} alt={alt ?? ""} title={title} referrerPolicy="no-referrer" />;
  }
  const label = (
    <>
      <ImageOff size={12} aria-hidden="true" />
      <span>{t("markdown.external_image_blocked")}</span>
      {decision.host && <span className="font-mono">{decision.host}</span>}
    </>
  );
  const className = "inline-flex items-center gap-1 rounded border border-border px-2 py-0.5 text-xs text-muted-foreground not-prose";
  if (!decision.href) return <span className={className}>{label}</span>;
  return (
    <a
      href={decision.href}
      target="_blank"
      rel="noopener noreferrer"
      title={alt || t("markdown.external_image_open")}
      className={`${className} no-underline hover:text-primary`}
    >
      {label}
    </a>
  );
}

/**
 * 顯示模型輸出的唯一 Markdown 元件（ADR-0294）：`img` 固定交給 `MarkdownImage`，呼叫端傳的 `img` 會被蓋掉；
 * 不收 rehype 外掛，原始 HTML 不會被顯示。
 */
export function SafeMarkdown({ children, components }: { children: string; components?: Components }) {
  const merged = React.useMemo<Components>(() => ({ ...components, img: MarkdownImage }), [components]);
  return (
    <ReactMarkdown remarkPlugins={REMARK_PLUGINS} components={merged}>
      {children}
    </ReactMarkdown>
  );
}
