import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Wrench, Search } from "lucide-react";
import { Tool } from "@/lib/types";

interface Props {
    systemTools: Tool[];
}

export function ToolsTab({ systemTools }: Props) {
    const { t } = useTranslation();
    const [search, setSearch] = useState("");
    const q = search.trim().toLowerCase();
    // 隱藏 MCP 類工具（另有專頁），再套用文字搜尋（名稱 / 描述 / 分類）。
    const filteredTools = systemTools
        .filter(tool => !tool.category?.startsWith("MCP"))
        .filter(tool =>
            !q ||
            tool.name.toLowerCase().includes(q) ||
            (tool.description || "").toLowerCase().includes(q) ||
            (tool.category || "").toLowerCase().includes(q)
        );

    return (
        <div className="space-y-4 animate-in fade-in slide-in-from-bottom-2 duration-300">
            <div className="relative">
                <Search className="text-muted-foreground absolute top-1/2 left-4 h-4 w-4 -translate-y-1/2" />
                <input
                    type="text"
                    aria-label={t('settings.tools_tab.search_placeholder')}
                    placeholder={t('settings.tools_tab.search_placeholder')}
                    value={search}
                    onChange={e => setSearch(e.target.value)}
                    className="bg-muted/20 border-input focus:ring-primary/50 w-full rounded-2xl border py-3 pr-4 pl-12 text-sm transition-all outline-none focus:ring-2"
                />
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {filteredTools.length > 0 ? filteredTools.map(tool => (
                    <div key={tool.name} className="p-5 bg-muted/30 rounded-3xl border border-border flex items-start gap-4 hover:border-primary/40 transition-all hover:shadow-md group">
                        <div className="p-3 bg-primary/10 rounded-2xl text-primary transition-colors group-hover:bg-primary-button group-hover:text-white">
                            <Wrench size={20}/>
                        </div>
                        <div className="min-w-0 flex-1">
                            <div className="flex items-center justify-between gap-2 mb-1.5">
                                <div className="font-black text-sm text-foreground truncate uppercase tracking-tight">{tool.name.includes("__") ? tool.name.split("__")[1] : tool.name}</div>
                                {tool.category && (
                                    <span className="rounded-md border border-border bg-background px-1.5 py-0.5 whitespace-nowrap text-xs font-bold uppercase text-muted-foreground">
                                        {tool.category}
                                    </span>
                                )}
                            </div>
                            <p className="text-xs text-muted-foreground leading-relaxed line-clamp-3">{tool.description || t('settings.tools_tab.no_description')}</p>
                        </div>
                    </div>
                )) : (
                    <div className="col-span-2 rounded-3xl border-2 border-dashed border-border p-16 text-center text-muted-foreground">
                        <div className="flex flex-col items-center gap-3">
                            <Wrench size={32} className="opacity-20" />
                            <span className="text-sm font-bold">{q ? t('settings.tools_tab.no_results') : t('settings.tools_tab.empty_title')}</span>
                            {!q && <p className="text-xs opacity-60">{t('settings.tools_tab.empty_hint')}</p>}
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
}
