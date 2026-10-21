"use client";
import { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { BookOpen, X, Wrench, Server, FileText } from "lucide-react";
import { Skill, CatalogSkill } from "@/lib/types";
import {
    Dialog,
    DialogContent,
    DialogTitle,
    DialogDescription,
} from "@/components/ui/dialog";

interface Props {
    /** 已安裝技能（含 system_prompt body，可能延後載入）。 */
    skill?: Skill | null;
    /** 目錄技能（尚未安裝，僅有 metadata，無 body）。 */
    catalogSkill?: CatalogSkill | null;
    /** 已安裝技能的 body 仍在抓取中。 */
    bodyLoading?: boolean;
    onClose: () => void;
}

export default function SkillPreviewModal({ skill, catalogSkill, bodyLoading, onClose }: Props) {
    const { t } = useTranslation();
    const isCatalog = !!catalogSkill && !skill;
    const name = skill?.name ?? catalogSkill?.name ?? "";
    const description = skill?.description ?? catalogSkill?.description ?? "";

    return (
        <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
            <DialogContent
                overlayClassName="bg-background/80 backdrop-blur-sm"
                className="bg-card w-full max-w-2xl rounded-3xl border border-border shadow-2xl overflow-hidden flex flex-col max-h-[85vh]"
            >
                <div className="p-6 border-b border-border flex items-center justify-between shrink-0 bg-muted/20">
                    <div className="flex items-center gap-3 min-w-0">
                        <div className="p-2.5 bg-primary/10 text-primary rounded-xl shrink-0">
                            <BookOpen size={20} />
                        </div>
                        <div className="min-w-0">
                            <DialogTitle className="text-xl font-black text-foreground truncate">{name}</DialogTitle>
                            <DialogDescription className="text-xs text-muted-foreground">
                                {isCatalog
                                    ? t('modals.skill_preview_modal.catalog_subtitle')
                                    : t('modals.skill_preview_modal.installed_subtitle')}
                            </DialogDescription>
                        </div>
                    </div>
                    <button
                        onClick={onClose}
                        aria-label={t('modals.skill_preview_modal.close')}
                        className="p-2 hover:bg-muted rounded-full transition-colors shrink-0"
                    >
                        <X size={20} className="text-muted-foreground" />
                    </button>
                </div>

                <div className="p-6 overflow-y-auto custom-scrollbar flex-1 space-y-6">
                    {description && (
                        <p className="text-sm text-muted-foreground leading-relaxed">{description}</p>
                    )}

                    {isCatalog && catalogSkill && (
                        <div className="grid grid-cols-2 gap-3">
                            <Meta label={t('modals.skill_preview_modal.category_label')} value={catalogSkill.category} />
                            <Meta label={t('modals.skill_preview_modal.author_label')} value={catalogSkill.author} />
                            <Meta label={t('modals.skill_preview_modal.license_label')} value={catalogSkill.license} />
                            <Meta label={t('modals.skill_preview_modal.source_label')} value={`${catalogSkill.repo}@${catalogSkill.commit.slice(0, 7)}`} />
                        </div>
                    )}

                    {!isCatalog && skill && (
                        <div className="space-y-4">
                            <ChipList icon={<Wrench size={14} />} label={t('modals.skill_preview_modal.required_tools_label')} items={skill.required_tools ?? []} empty={t('modals.skill_preview_modal.none')} />
                            <ChipList icon={<Server size={14} />} label={t('modals.skill_preview_modal.mcp_servers_label')} items={skill.mcp_servers ?? []} empty={t('modals.skill_preview_modal.none')} />
                        </div>
                    )}

                    {isCatalog && catalogSkill ? (
                        <div className="space-y-3">
                            {catalogSkill.files.length > 0 && (
                                <div>
                                    <h5 className="text-xs font-black uppercase tracking-wider text-muted-foreground mb-2 flex items-center gap-1.5">
                                        <FileText size={14} /> {t('modals.skill_preview_modal.files_label')}
                                    </h5>
                                    <div className="rounded-xl border border-border bg-muted/20 p-3 font-mono text-xs text-muted-foreground space-y-1 max-h-40 overflow-y-auto custom-scrollbar">
                                        {catalogSkill.files.map(f => <div key={f.path} className="truncate">{f.path}</div>)}
                                    </div>
                                </div>
                            )}
                            <p className="rounded-xl border border-primary/20 bg-primary/5 p-3 text-xs text-primary">
                                {t('modals.skill_preview_modal.catalog_body_hint')}
                            </p>
                        </div>
                    ) : (
                        <div>
                            <h5 className="text-xs font-black uppercase tracking-wider text-muted-foreground mb-2">
                                {t('modals.skill_preview_modal.instructions_label')}
                            </h5>
                            {bodyLoading ? (
                                <div className="text-xs text-muted-foreground italic">{t('modals.skill_preview_modal.loading_instructions')}</div>
                            ) : skill?.system_prompt ? (
                                <pre className="rounded-2xl border border-border bg-muted/30 p-4 text-xs leading-relaxed text-foreground whitespace-pre-wrap break-words max-h-80 overflow-y-auto custom-scrollbar">{skill.system_prompt}</pre>
                            ) : (
                                <div className="text-xs text-muted-foreground italic">{t('modals.skill_preview_modal.no_instructions')}</div>
                            )}
                        </div>
                    )}
                </div>

                <div className="p-6 border-t border-border flex items-center justify-end shrink-0 bg-muted">
                    <button
                        onClick={onClose}
                        className="px-6 py-2.5 rounded-xl font-bold bg-primary-button text-primary-foreground hover:opacity-90 transition-all text-sm"
                    >
                        {t('modals.skill_preview_modal.close')}
                    </button>
                </div>
            </DialogContent>
        </Dialog>
    );
}

function Meta({ label, value }: { label: string; value: string }) {
    return (
        <div className="rounded-xl border border-border bg-muted/20 p-3">
            <div className="text-xs font-black uppercase tracking-wider text-muted-foreground">{label}</div>
            <div className="mt-1 truncate font-mono text-xs text-foreground">{value}</div>
        </div>
    );
}

function ChipList({ icon, label, items, empty }: { icon: ReactNode; label: string; items: string[]; empty: string }) {
    return (
        <div>
            <h5 className="text-xs font-black uppercase tracking-wider text-muted-foreground mb-2 flex items-center gap-1.5">
                {icon} {label}
            </h5>
            {items.length > 0 ? (
                <div className="flex flex-wrap gap-1.5">
                    {items.map(it => <span key={it} className="rounded-md border border-border bg-muted px-2 py-0.5 font-mono text-xs text-muted-foreground">{it}</span>)}
                </div>
            ) : (
                <span className="text-xs text-muted-foreground italic">{empty}</span>
            )}
        </div>
    );
}
