"use client";

/**
 * ADR-0317：審批卡上的「小幫手掃描到的清單」。
 *
 * 內容是小幫手在使用者電腦上掃到的檔名與動作，和小幫手視窗的格式相同；等寬字、可以捲動。
 * 卡片放不下時說明還要在小幫手視窗再確認；小幫手會再跳視窗時另外說一句。
 */

import { useTranslation } from "react-i18next";
import { FolderSearch } from "lucide-react";
import type { ToolStepPreview } from "@/lib/tree_history";

export function HelperPreview({ preview }: { preview: ToolStepPreview }) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-1.5 text-xs font-bold opacity-80">
        <FolderSearch size={12} className="shrink-0" />
        <span>{t("chat.tool_approval_card.preview_title")}</span>
      </div>
      <pre
        data-testid="helper-preview"
        className="max-h-80 overflow-auto custom-scrollbar rounded-lg bg-background/60 p-2 text-xs font-mono whitespace-pre"
      >
        {preview.text}
      </pre>
      {!preview.complete && (
        <p className="text-xs text-status-warning">{t("chat.tool_approval_card.preview_incomplete")}</p>
      )}
      {preview.helperWindow && (
        <p className="text-xs opacity-80">{t("chat.tool_approval_card.preview_helper_window")}</p>
      )}
    </div>
  );
}
