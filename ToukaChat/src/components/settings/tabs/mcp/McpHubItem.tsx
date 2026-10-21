import React from "react";
import { useTranslation } from "react-i18next";
import {
  Globe, Box, Search, Github, Folder, Brain,
  Database, Download, MessageSquare, Home, ShieldAlert,
  Check, ExternalLink, ArrowUpCircle,
  FileText, ShieldCheck, CreditCard, Palette, BookOpen, Bug, Ticket
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { McpStatus, McpHubServerView, formatHubDownloads } from "@/lib/mcp_version";

/**
 * Hub 條目 `icon` 字串 → lucide 元件。
 *
 * **必須涵蓋後端種子（`ToukaProxy/config/mcp_hub.json.example`）用到的每一個 icon 值**
 * ——缺鍵時本元件靜默 fallback 成 `Box`，屬「無聲失效」家族（ADR-0191 補齊 `FileText`／
 * `ShieldCheck` 兩個既有缺鍵；跨層涵蓋由 `mcp_hub_icons.test.ts` 讀種子檔守衛）。
 */
export const IconMap: Record<string, React.ElementType> = {
    Search: Search, Github: Github, Folder: Folder,
    Globe: Globe, Brain: Brain, Database: Database, Download: Download,
    Box: Box, MessageSquare: MessageSquare, Edit: Home, ShieldAlert: ShieldAlert,
    FileText: FileText, ShieldCheck: ShieldCheck, CreditCard: CreditCard,
    Palette: Palette, BookOpen: BookOpen, Bug: Bug, Ticket: Ticket
};

interface Props {
  hubSrv: McpHubServerView;
  status: McpStatus;
  onInstall: (server: McpHubServerView) => void;
}

export function McpHubItem({ hubSrv, status, onInstall }: Props) {
    const { t } = useTranslation();
    const IconComponent = hubSrv.icon ? IconMap[hubSrv.icon] : Box;
    const canUpdate = status === "update_available";
    const upToDate = status === "up_to_date";

    return (
        <div className="relative flex flex-col overflow-hidden rounded-3xl border border-border bg-card p-5 transition-all hover:border-primary/40 hover:shadow-xl group">
            <div className="flex items-start justify-between mb-4">
                <div className="w-12 h-12 bg-primary/10 text-primary rounded-2xl flex items-center justify-center group-hover:bg-primary-button group-hover:text-white transition-colors duration-300">
                    {IconComponent ? <IconComponent size={24} /> : <Box size={24} />}
                </div>
                {status === "not_installed" ? (
                    <div className="flex items-center gap-1.5 rounded-lg border border-primary/20 bg-primary/10 px-2.5 py-1 text-primary shadow-sm">
                        <Download size={10} />
                        <span className="text-xs font-black leading-none tracking-widest">
                            {formatHubDownloads(hubSrv.downloads)}
                        </span>
                    </div>
                ) : canUpdate ? (
                    <span className="inline-flex items-center gap-1 rounded-lg bg-primary/10 px-2.5 py-1 text-xs font-black text-primary">
                        <ArrowUpCircle size={12} /> {t('settings.mcp_hub_item.badge_update_available')}
                    </span>
                ) : (
                    <span className="rounded-lg bg-muted px-2.5 py-1 text-xs font-black text-muted-foreground">{t('settings.mcp_hub_item.badge_installed')}</span>
                )}
            </div>
            <div className="flex items-center justify-between mb-1">
                <div className="font-black text-base text-foreground truncate pr-2">{hubSrv.name}</div>
                {hubSrv.source_url && (
                    <a
                        href={hubSrv.source_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="p-1.5 text-muted-foreground hover:text-primary transition-colors hover:bg-primary/5 rounded-lg"
                        title={t('settings.mcp_hub_item.view_source_title')}
                        onClick={(e) => e.stopPropagation()}
                    >
                        <ExternalLink size={14} />
                    </a>
                )}
            </div>
            <div className="text-xs font-bold uppercase text-muted-foreground opacity-80">{hubSrv.category}</div>
            <p className="text-xs text-muted-foreground mt-3 leading-relaxed flex-1 line-clamp-3">{hubSrv.description}</p>

            <Button
                variant={upToDate ? "secondary" : "default"}
                onClick={() => { if (status === "not_installed" || canUpdate) onInstall(hubSrv); }}
                disabled={upToDate}
                className={cn(
                    "mt-5 w-full py-5 rounded-xl text-sm font-bold transition-all flex items-center justify-center gap-2",
                    upToDate ? "bg-muted text-muted-foreground opacity-50 cursor-not-allowed" : "bg-primary/10 text-primary hover:bg-primary-button hover:text-white"
                )}
            >
                {status === "not_installed" ? <Download size={16} /> : canUpdate ? <ArrowUpCircle size={16} /> : <Check size={16} />}
                {status === "not_installed" ? t('settings.mcp_hub_item.action_install') : canUpdate ? t('settings.mcp_hub_item.action_update') : t('settings.mcp_hub_item.action_installed')}
            </Button>
        </div>
    );
}
