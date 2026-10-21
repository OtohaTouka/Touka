import { useState } from "react";
import { useTranslation } from "react-i18next";
import { PlusCircle, Download, Search } from "lucide-react";
import { Skill } from "@/lib/types";
import { SkillItem } from "./SkillItem";

interface Props {
    skills: Skill[];
    onEdit: (skill: Skill) => void;
    onDelete: (id: string) => void;
    onExport: (skill: Skill) => void;
    onAddNew: () => void;
    onInstallFromUrl: () => void;
    onPreview: (skill: Skill) => void;
}

export function MySkillsView({ skills, onEdit, onDelete, onExport, onAddNew, onInstallFromUrl, onPreview }: Props) {
    const { t } = useTranslation();
    const [search, setSearch] = useState("");
    const q = search.trim().toLowerCase();
    const filtered = skills.filter(s =>
        !q || s.name.toLowerCase().includes(q) || (s.description || "").toLowerCase().includes(q)
    );
    return (
        <div className="flex flex-col gap-4 animate-in fade-in duration-300">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                <div className="relative flex-1 w-full">
                    <Search className="text-muted-foreground absolute top-1/2 left-4 h-4 w-4 -translate-y-1/2" />
                    <input
                        type="text"
                        aria-label={t('settings.my_skills_view.search_placeholder')}
                        placeholder={t('settings.my_skills_view.search_placeholder')}
                        value={search}
                        onChange={e => setSearch(e.target.value)}
                        className="bg-muted/20 border-input focus:ring-primary/50 w-full rounded-2xl border py-3 pr-4 pl-12 text-sm transition-all outline-none focus:ring-2"
                    />
                </div>
                <button
                    onClick={onInstallFromUrl}
                    className="flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-bold uppercase tracking-widest text-primary hover:bg-primary/10 transition-colors shrink-0"
                >
                    <Download size={14} /> {t('settings.my_skills_view.install_from_url')}
                </button>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <button
                    type="button"
                    onClick={onAddNew}
                    className="group flex min-h-40 w-full cursor-pointer flex-col items-center justify-center gap-2 rounded-3xl border-2 border-dashed border-border p-5 text-muted-foreground transition-all hover:border-primary/50 hover:bg-primary/5 hover:text-primary outline-none focus-visible:ring-2 focus-visible:ring-primary/50 focus-visible:ring-offset-2"
                >
                    <PlusCircle size={24} className="group-hover:scale-110 transition-transform" />
                    <span className="text-xs font-bold uppercase tracking-widest">{t('settings.my_skills_view.add_custom_skill')}</span>
                </button>
                {filtered.map(skill => (
                    <SkillItem
                        key={skill.id}
                        skill={skill}
                        onEdit={onEdit}
                        onDelete={onDelete}
                        onExport={onExport}
                        onPreview={onPreview}
                    />
                ))}
            </div>
        </div>
    );
}
