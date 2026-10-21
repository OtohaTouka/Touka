import { useTranslation } from "react-i18next";
import { Zap, ShieldCheck, Settings2, Trash2, Download, Eye } from "lucide-react";
import { cn } from "@/lib/utils";
import { Skill } from "@/lib/types";
import { Button } from "@/components/ui/button";

interface Props {
    skill: Skill;
    onEdit: (skill: Skill) => void;
    onDelete: (id: string) => void;
    onExport: (skill: Skill) => void;
    onPreview: (skill: Skill) => void;
}

export function SkillItem({ skill, onEdit, onDelete, onExport, onPreview }: Props) {
    const { t } = useTranslation();
    return (
        <div className="relative flex flex-col gap-3 overflow-hidden rounded-3xl border border-border bg-muted/30 p-5 transition-all hover:bg-muted/50 hover:shadow-md group">
            <div className="flex items-start justify-between relative z-10">
                <div className={cn(
                    "p-2.5 rounded-2xl shadow-sm border transition-all duration-300",
                    skill.is_system 
                        ? "bg-primary/10 text-primary border-primary/20 group-hover:bg-primary-button group-hover:text-white" 
                        : "bg-primary/10 text-primary border-primary/10 group-hover:bg-primary-button group-hover:text-white"
                )}>
                    {skill.is_system ? <ShieldCheck size={20} fill="currentColor" /> : <Zap size={20} fill="currentColor" />}
                </div>
                <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                    <Button
                        variant="ghost"
                        size="icon"
                        aria-label={t('settings.skill_item.preview_aria', { name: skill.name })}
                        onClick={() => onPreview(skill)}
                        className="w-9 h-9 hover:bg-primary/10 text-primary rounded-xl transition-colors"
                    >
                        <Eye size={16}/>
                    </Button>
                    {skill.is_system ? (
                        <div className="rounded-lg border border-primary/20 bg-primary/10 px-2 py-1 text-xs font-black uppercase tracking-widest text-primary">
                            {t('settings.skill_item.system_protected_badge')}
                        </div>
                    ) : (
                        <>
                            <Button
                                variant="ghost"
                                size="icon"
                                aria-label={t('settings.skill_item.export_skill_tooltip')}
                                onClick={() => onExport(skill)}
                                className="w-9 h-9 hover:bg-primary/10 text-primary rounded-xl transition-colors"
                            >
                                <Download size={16}/>
                            </Button>
                            <Button
                                variant="ghost"
                                size="icon"
                                aria-label={t('settings.skill_item.edit_skill_aria', { name: skill.name })}
                                onClick={() => onEdit(skill)}
                                className="w-9 h-9 hover:bg-primary/10 text-primary rounded-xl transition-colors"
                            >
                                <Settings2 size={16}/>
                            </Button>
                            <Button
                                variant="ghost"
                                size="icon"
                                aria-label={t('settings.skill_item.delete_skill_aria', { name: skill.name })}
                                onClick={() => onDelete(skill.id)}
                                className="w-9 h-9 hover:bg-destructive/10 text-destructive rounded-xl transition-colors"
                            >
                                <Trash2 size={16}/>
                            </Button>
                        </>
                    )}
                </div>
            </div>
            <div className="relative z-10">
                <div className="font-black text-base text-foreground uppercase tracking-tight group-hover:text-primary transition-colors">
                    {skill.name}
                </div>
                <p className="text-xs text-muted-foreground leading-relaxed mt-2 line-clamp-2">
                    {skill.description}
                </p>
            </div>
            <div className="absolute -right-4 -bottom-4 text-primary/5 opacity-0 group-hover:opacity-100 transition-all duration-500 transform group-hover:-translate-x-4 group-hover:-translate-y-4">
                <Zap size={80} fill="currentColor" />
            </div>
        </div>
    );
}
