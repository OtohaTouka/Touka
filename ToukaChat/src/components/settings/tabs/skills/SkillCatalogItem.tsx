import { useTranslation } from "react-i18next";
import { Wrench, ArrowUpCircle, Eye } from "lucide-react";
import { cn } from "@/lib/utils";
import { CatalogSkill } from "@/lib/types";
import { CatalogStatus } from "@/lib/skill_version";
import { Button } from "@/components/ui/button";

interface Props {
  catalogSkill: CatalogSkill;
  status: CatalogStatus;
  onInstall: (catalogSkill: CatalogSkill, isUpgrade: boolean) => void;
  onPreview: (catalogSkill: CatalogSkill) => void;
}


export function SkillCatalogItem({ catalogSkill, status, onInstall, onPreview }: Props) {
    const { t } = useTranslation();
    const shortCommit = catalogSkill.commit.slice(0, 7);
    const canUpgrade = status === "update_available";
    const upToDate = status === "up_to_date";
    return (
        <div className="relative flex flex-col gap-4 rounded-3xl border border-border bg-card p-6 transition-all hover:border-primary/30 hover:shadow-xl hover:shadow-primary/5 group">
            <div className="flex items-start justify-between">
                <div className="p-3 bg-primary/5 text-primary rounded-2xl group-hover:bg-primary-button group-hover:text-white transition-all">
                    <Wrench size={24} />
                </div>
                <div className="flex items-center gap-2">
                    <Button
                        variant="ghost"
                        size="icon"
                        aria-label={t('settings.skill_catalog_item.preview_aria', { name: catalogSkill.name })}
                        onClick={() => onPreview(catalogSkill)}
                        className="h-8 w-8 rounded-lg text-muted-foreground transition-colors hover:bg-primary/10 hover:text-primary"
                    >
                        <Eye size={16} />
                    </Button>
                    {canUpgrade && (
                        <span className="inline-flex items-center gap-1 rounded-lg bg-primary/10 px-2 py-1 text-xs font-bold text-primary">
                            <ArrowUpCircle size={12} /> {t('settings.skill_catalog_item.update_available_badge')}
                        </span>
                    )}
                    <div className="rounded-lg bg-muted/40 px-2 py-1 font-mono text-xs text-muted-foreground">
                        {catalogSkill.license}
                    </div>
                </div>
            </div>
            <div>
                <div className="text-xs font-bold uppercase tracking-widest text-muted-foreground">{catalogSkill.category}</div>
                <h5 className="mt-1 truncate text-lg font-black text-foreground">{catalogSkill.name}</h5>
                <p className="mt-3 h-16 text-xs leading-relaxed text-muted-foreground line-clamp-3">
                    {catalogSkill.description}
                </p>
                <div className="mt-4 w-full truncate rounded-lg bg-muted/30 px-2 py-1 font-mono text-xs text-muted-foreground">
                    {catalogSkill.author} · {catalogSkill.repo}@{shortCommit}
                </div>
            </div>
            <Button
                variant={upToDate ? "secondary" : "default"}
                onClick={() => {
                    if (status === "not_installed") onInstall(catalogSkill, false);
                    else if (canUpgrade) onInstall(catalogSkill, true);
                }}
                disabled={upToDate}
                className={cn(
                    "mt-1 w-full rounded-2xl py-6 text-xs font-black uppercase tracking-widest transition-all",
                    upToDate
                        ? "cursor-not-allowed bg-muted text-muted-foreground"
                        : "bg-primary-button text-white shadow-lg shadow-primary/20 hover:opacity-90 active:scale-95"
                )}
            >
                {status === "not_installed"
                    ? t('settings.skill_catalog_item.install_button')
                    : upToDate
                        ? t('settings.skill_catalog_item.up_to_date_button')
                        : t('settings.skill_catalog_item.upgrade_button', { commit: shortCommit })}
            </Button>
        </div>
    );
}
