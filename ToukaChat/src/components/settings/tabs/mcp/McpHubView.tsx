import { useState, useMemo } from "react";
import { useTranslation } from "react-i18next";
import { RefreshCw, Box, Search } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { computeMcpStatus, McpHubServerView, InstalledMcpConfig } from "@/lib/mcp_version";
import { McpHubItem } from "./McpHubItem";

interface Props {
    mcpConfig: InstalledMcpConfig;
    mcpHubServers: McpHubServerView[];
    syncMcpHub: () => Promise<boolean>;
    setSelectedHubServer: (server: McpHubServerView | null) => void;
}

export function McpHubView({ mcpConfig, mcpHubServers, syncMcpHub, setSelectedHubServer }: Props) {
    const { t } = useTranslation();
    const [mcpHubCategory, setMcpHubCategory] = useState<string>("All");
    const [searchTerm, setSearchTerm] = useState("");
    const [isHubSyncing, setIsHubSyncing] = useState(false);

    const handleSync = async () => {
        setIsHubSyncing(true);
        await syncMcpHub();
        setIsHubSyncing(false);
    };

    // 分類頁籤由 hub 資料衍生（不硬編）→ 永遠與後端 category 一致，杜絕漂移與空白頁籤。
    const categories = useMemo(
        () => [
            "All",
            ...Array.from(
                new Set(mcpHubServers.map((s) => s.category).filter((c): c is string => Boolean(c))),
            ).sort(),
        ],
        [mcpHubServers],
    );

    // 分類過濾 ＋ 文字搜尋（名稱／描述）兩條件獨立組合。
    const filtered = useMemo(() => {
        const q = searchTerm.trim().toLowerCase();
        return mcpHubServers
            .filter(srv => mcpHubCategory === "All" || srv.category === mcpHubCategory)
            .filter(srv =>
                !q ||
                srv.name?.toLowerCase().includes(q) ||
                srv.description?.toLowerCase().includes(q)
            );
    }, [mcpHubServers, mcpHubCategory, searchTerm]);

    return (
        <div className="space-y-6">
            <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between border-b border-border pb-4">
                <div className="flex items-center gap-3">
                    <h4 className="text-sm font-bold text-foreground">{t('settings.mcp_hub_view.heading')}</h4>
                    <Button
                        variant="ghost"
                        size="icon"
                        aria-label={t('settings.mcp_hub_view.sync_aria')}
                        onClick={handleSync}
                        disabled={isHubSyncing}
                        className={cn(
                            "w-8 h-8 rounded-lg transition-all",
                            isHubSyncing ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground hover:bg-primary/10 hover:text-primary"
                        )}
                    >
                        <RefreshCw size={14} className={cn(isHubSyncing && "animate-spin")} />
                    </Button>
                </div>
                
                <div className="flex bg-muted/60 p-1 rounded-xl items-center text-xs overflow-x-auto self-stretch sm:self-auto custom-scrollbar shadow-inner border border-border">
                    {categories.map(cat => (
                        <button 
                            key={cat} 
                            onClick={() => setMcpHubCategory(cat)}
                            className={cn(
                                "px-4 py-1.5 rounded-lg font-bold transition-all whitespace-nowrap", 
                                mcpHubCategory === cat 
                                    ? "bg-background text-foreground shadow-sm border border-border/80" 
                                    : "text-muted-foreground hover:text-foreground hover:bg-muted"
                            )}
                        >
                            {cat === "All" ? t('settings.mcp_hub_view.category_all') : cat}
                        </button>
                    ))}
                </div>
            </div>

            <div className="relative">
                <Search className="text-muted-foreground absolute top-1/2 left-4 h-4 w-4 -translate-y-1/2" />
                <input
                    type="text"
                    aria-label={t('settings.mcp_hub_view.search_placeholder')}
                    placeholder={t('settings.mcp_hub_view.search_placeholder')}
                    value={searchTerm}
                    onChange={(e) => setSearchTerm(e.target.value)}
                    className="bg-muted/40 border-input focus:ring-primary/50 w-full rounded-2xl border py-3 pr-4 pl-12 text-sm transition-all outline-none focus:ring-2"
                />
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-5">
                {mcpHubServers.length === 0 ? (
                    <div className="col-span-full py-10 text-center text-xs text-muted-foreground italic">
                        {t('settings.mcp_hub_view.hub_unreachable')}
                    </div>
                ) : filtered.length > 0 ? (
                    filtered.map(hubSrv => (
                        <McpHubItem
                            key={hubSrv.id}
                            hubSrv={hubSrv}
                            status={computeMcpStatus(hubSrv, mcpConfig)}
                            onInstall={setSelectedHubServer}
                        />
                    ))
                ) : (
                    <div className="col-span-full space-y-4 rounded-3xl border-2 border-dashed border-border/50 bg-muted/10 py-16 text-center">
                        <div className="w-16 h-16 bg-muted/20 rounded-full flex items-center justify-center mx-auto mb-2 text-muted-foreground">
                            <Box size={32} />
                        </div>
                        <div className="text-sm font-bold text-muted-foreground">{searchTerm.trim() ? t('settings.mcp_hub_view.empty_search') : t('settings.mcp_hub_view.empty_category')}</div>
                        <Button
                            variant="link"
                            onClick={() => { setMcpHubCategory("All"); setSearchTerm(""); }}
                            className="text-xs font-black text-primary uppercase tracking-widest"
                        >
                            {t('settings.mcp_hub_view.switch_to_all')}
                        </Button>
                    </div>
                )}
            </div>
        </div>
    );
}
