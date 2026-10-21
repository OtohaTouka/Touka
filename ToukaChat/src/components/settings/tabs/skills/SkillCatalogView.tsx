import { useState, useMemo } from "react";
import { useTranslation } from "react-i18next";
import { ShieldAlert, Search } from "lucide-react";
import { cn } from "@/lib/utils";
import { Skill, CatalogSkill } from "@/lib/types";
import { computeCatalogStatus } from "@/lib/skill_version";
import { SkillCatalogItem } from "./SkillCatalogItem";

interface Props {
  skills: Skill[];
  catalogItems: CatalogSkill[];
  onInstall: (catalogSkill: CatalogSkill, isUpgrade: boolean) => void;
  onPreview: (catalogSkill: CatalogSkill) => void;
}


export function SkillCatalogView({ skills, catalogItems, onInstall, onPreview }: Props) {
    const { t } = useTranslation();
    const [selectedCategory, setSelectedCategory] = useState("All");
    const [search, setSearch] = useState("");
    const categories = ["All", "Dev", "Office", "Writing", "Creative", "Life"];

    // 分類 ＋ 文字搜尋（名稱／描述）獨立組合。
    const filtered = useMemo(() => {
        const q = search.trim().toLowerCase();
        return catalogItems.filter(item =>
            (selectedCategory === "All" || item.category === selectedCategory) &&
            (!q || item.name.toLowerCase().includes(q) || (item.description || "").toLowerCase().includes(q))
        );
    }, [catalogItems, selectedCategory, search]);

    return (
        <div className="space-y-6 animate-in fade-in duration-300">
            <div className="flex items-start gap-2 rounded-2xl border border-destructive/30 bg-destructive/10 p-4 text-xs leading-relaxed text-destructive">
                <ShieldAlert size={16} className="mt-0.5 shrink-0" />
                <span>
                    {t('settings.skill_catalog_view.security_warning')}
                </span>
            </div>

            <div className="relative">
                <Search className="text-muted-foreground absolute top-1/2 left-4 h-4 w-4 -translate-y-1/2" />
                <input
                    type="text"
                    aria-label={t('settings.skill_catalog_view.search_placeholder')}
                    placeholder={t('settings.skill_catalog_view.search_placeholder')}
                    value={search}
                    onChange={e => setSearch(e.target.value)}
                    className="bg-muted/20 border-input focus:ring-primary/50 w-full rounded-2xl border py-3 pr-4 pl-12 text-sm transition-all outline-none focus:ring-2"
                />
            </div>

            <div className="flex flex-wrap gap-2">
                {categories.map(cat => (
                    <button
                        key={cat}
                        onClick={() => setSelectedCategory(cat)}
                        className={cn(
                            "rounded-xl border px-4 py-1.5 text-xs font-black uppercase tracking-widest transition-all",
                            selectedCategory === cat
                                ? "bg-primary border-primary text-white shadow-lg shadow-primary/20"
                                : "bg-card border-border text-muted-foreground hover:border-primary/50"
                        )}
                    >
                        {t(`settings.skill_catalog_view.category_${cat.toLowerCase()}`)}
                    </button>
                ))}
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
                {filtered.map(item => (
                    <SkillCatalogItem
                        key={item.id}
                        catalogSkill={item}
                        status={computeCatalogStatus(item, skills)}
                        onInstall={onInstall}
                        onPreview={onPreview}
                    />
                ))}
                {filtered.length === 0 && (
                    <div className="col-span-full rounded-3xl border-2 border-dashed border-border/50 bg-muted/10 py-16 text-center text-xs italic text-muted-foreground">
                        {search.trim() ? t('settings.skill_catalog_view.no_results') : t('settings.skill_catalog_view.empty_state')}
                    </div>
                )}
            </div>
        </div>
    );
}
