"use client";

import { useState, useEffect } from 'react';
import { useTranslation } from "react-i18next";
import { Folder, Plus, Calendar, HardDrive, Trash2, AlertCircle } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useChat } from '@/lib/providers/ChatProvider';
import { useModels } from '@/lib/providers/ModelProvider';
import { getSystemEpoch, formatToTaipeiDate } from '@/lib/time_utils';

import { toast } from 'sonner';
import { useProjects, Project } from '@/hooks/useProjects';
import { useConfirmToast } from '@/hooks/useConfirmToast';

export function ProjectsView() {
    const { t } = useTranslation();
    const { handleNewSession } = useChat();
    const { availableModels } = useModels();
    const { projects, isLoading, fetchProjects, saveProject, deleteProject } = useProjects();
    const { confirmToast } = useConfirmToast();
    const [isAdding, setIsAdding] = useState(false);
    
    // Form state
    const [form, setForm] = useState({
        name: '',
        root_path: '',
        description: ''
    });

    useEffect(() => {
        fetchProjects();
    }, [fetchProjects]);

    const handleSave = async () => {
        if (!form.name || !form.root_path) return;
        
        const success = await saveProject({
            id: `proj-${getSystemEpoch()}`,
            ...form
        });
        
        if (success) {
            setIsAdding(false);
            setForm({ name: '', root_path: '', description: '' });
        } else {
            // D-fix: 後端拒絕（403 權限 / 422 越界 root_path / 5xx）時表單保持開啟並提示，
            // 不再靜默假成功（saveProject 現依 res.ok 誠實回傳）。
            toast.error(t('projects.projects_view.save_failed'));
        }
    };

    const handleDelete = (id: string) => {
        confirmToast(
            t('projects.projects_view.delete_confirm_message'),
            async () => {
                // D-fix: 顯式檢查刪除結果，失敗時提示（deleteProject 現依 res.ok 誠實回傳）。
                const ok = await deleteProject(id);
                if (!ok) toast.error(t('projects.projects_view.delete_failed'));
            },
            // P-fix: confirmToast 的第 3 參數是「確認按鈕標籤」而非標題；改用語意正確的鍵。
            t('projects.projects_view.delete_confirm_button'),
        );
    };


    const startProjectSession = (project: Project) => {
        if (availableModels.length > 0) {
            handleNewSession(project.id);
            // 觸發視圖切換事件
            window.dispatchEvent(new CustomEvent("touka_switch_view", { detail: "chat" }));
        } else {
            toast.error(t('projects.projects_view.no_model_alert'));
        }
    };

  return (
        <div className="flex-1 overflow-y-auto p-8 bg-background/50">
            <div className="max-w-6xl mx-auto">
                <div className="flex items-center justify-between mb-8">
                    <div>
                        <h1 className="text-3xl font-black tracking-tight text-foreground flex items-center gap-3">
                            <Folder className="text-primary" size={32} />
                            {t('projects.projects_view.page_title')}
                        </h1>
                        <p className="text-muted-foreground mt-2 font-medium">{t('projects.projects_view.page_subtitle')}</p>
                    </div>
                    <Button 
                        onClick={() => setIsAdding(true)}
                        className="rounded-xl px-6 py-6 h-auto font-bold text-base shadow-lg shadow-primary/20 hover:scale-105 transition-transform gap-2"
                    >
                        <Plus size={20} /> {t('projects.projects_view.add_project_button')}
                    </Button>
                </div>

                {isAdding && (
                    <div className="mb-8 p-6 bg-card rounded-2xl border border-primary/20 shadow-xl animate-in fade-in slide-in-from-top-4 duration-300">
                        <h3 className="text-lg font-bold mb-4 flex items-center gap-2">
                            <Plus className="text-primary" size={20} /> {t('projects.projects_view.create_form_title')}
                        </h3>
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-4">
                            <div className="space-y-2">
                                <label className="text-xs font-bold uppercase tracking-wider text-muted-foreground">{t('projects.projects_view.field_name_label')}</label>
                                <input
                                    className="w-full bg-muted border-none rounded-xl p-3 focus:ring-2 ring-primary transition-all text-foreground"
                                    placeholder={t('projects.projects_view.field_name_placeholder')}
                                    value={form.name}
                                    onChange={e => setForm({...form, name: e.target.value})}
                                />
                            </div>
                            <div className="space-y-2">
                                <label className="text-xs font-bold uppercase tracking-wider text-muted-foreground">{t('projects.projects_view.field_root_path_label')}</label>
                                <input
                                    className="w-full bg-muted border-none rounded-xl p-3 focus:ring-2 ring-primary transition-all text-foreground"
                                    placeholder={t('projects.projects_view.field_root_path_placeholder')}
                                    value={form.root_path}
                                    onChange={e => setForm({...form, root_path: e.target.value})}
                                />
                            </div>
                        </div>
                        <div className="space-y-2 mb-6">
                            <label className="text-xs font-bold uppercase tracking-wider text-muted-foreground">{t('projects.projects_view.field_description_label')}</label>
                            <textarea
                                className="w-full bg-muted border-none rounded-xl p-3 focus:ring-2 ring-primary transition-all h-28 text-foreground"
                                placeholder={t('projects.projects_view.field_description_placeholder')}
                                value={form.description}
                                onChange={e => setForm({...form, description: e.target.value})}
                            />
                        </div>
                        <div className="flex justify-end gap-3">
                            <Button variant="ghost" onClick={() => setIsAdding(false)} className="rounded-xl font-bold">{t('projects.projects_view.cancel_button')}</Button>
                            <Button onClick={handleSave} className="rounded-xl px-8 font-bold">{t('projects.projects_view.save_button')}</Button>
                        </div>
                    </div>
                )}

                {isLoading ? (
                    <div className="flex flex-col items-center justify-center py-20 opacity-50">
                        <div className="animate-spin rounded-full h-12 w-12 border-4 border-primary border-t-transparent mb-4"></div>
                        <p className="font-bold">{t('projects.projects_view.loading')}</p>
                    </div>
                ) : projects.length === 0 && !isAdding ? (
                    <div className="text-center py-20 bg-card/50 rounded-3xl border-2 border-dashed border-border">
                        <div className="bg-muted w-20 h-20 rounded-full flex items-center justify-center mx-auto mb-6 text-muted-foreground">
                            <HardDrive size={40} />
                        </div>
                        <h3 className="text-xl font-bold mb-2 text-foreground">{t('projects.projects_view.empty_title')}</h3>
                        <p className="text-muted-foreground mb-8 max-w-md mx-auto">{t('projects.projects_view.empty_description')}</p>
                        <Button onClick={() => setIsAdding(true)} variant="outline" className="rounded-xl font-bold gap-2">
                            <Plus size={18} /> {t('projects.projects_view.empty_start_button')}
                        </Button>
                    </div>
                ) : (
                    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
                        {projects.map(project => (
                            <div key={project.id} className="group relative bg-card rounded-3xl border border-border p-6 shadow-sm hover:shadow-xl hover:border-primary/30 transition-all duration-300">
                                <div className="flex items-start justify-between mb-4">
                                    <div className="bg-primary/10 p-3 rounded-2xl text-primary group-hover:scale-110 transition-transform duration-300">
                                        <Folder size={24} />
                                    </div>
                                    <div className="flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                                        <Button
                                            variant="ghost"
                                            size="icon"
                                            aria-label={t('projects.projects_view.delete_project_aria', { name: project.name })}
                                            onClick={() => handleDelete(project.id)}
                                            className="h-9 w-9 text-muted-foreground hover:text-destructive hover:bg-destructive/10 rounded-lg"
                                        >
                                            <Trash2 size={18} />
                                        </Button>
                                    </div>
                                </div>
                                <h3 className="text-xl font-bold mb-2 group-hover:text-primary transition-colors text-foreground">{project.name}</h3>
                                <div className="space-y-3 mb-6">
                                    <div className="flex items-center gap-2 text-sm text-muted-foreground font-medium bg-muted/50 p-2 rounded-lg truncate">
                                        <HardDrive size={14} className="shrink-0" />
                                        <span className="truncate">{project.root_path}</span>
                                    </div>
                                    {project.description && (
                                        <p className="text-sm text-muted-foreground line-clamp-2 h-10 leading-relaxed">
                                            {project.description}
                                        </p>
                                    )}
                                    <div className="flex items-center gap-2 text-xs font-bold uppercase tracking-widest text-muted-foreground">
                                        <Calendar size={12} />
                                        {t('projects.projects_view.updated_at', { date: formatToTaipeiDate(project.updatedAt) })}
                                    </div>
                                </div>
                                <div className="flex gap-2 pt-2 border-t border-border/50 mt-auto">
                                    <Button 
                                        className="flex-1 rounded-xl font-bold text-xs gap-2 py-5"
                                        onClick={() => startProjectSession(project)}
                                    >
                                        <Plus size={14} /> {t('projects.projects_view.open_session_button')}
                                    </Button>
                                </div>
                            </div>
                        ))}
                    </div>
                )}

                <div className="mt-12 p-6 rounded-3xl bg-accent/10 border border-primary/20 flex items-start gap-4">
                    <div className="p-3 bg-primary/10 rounded-2xl text-primary shrink-0">
                        <AlertCircle size={24} />
                    </div>
                    <div>
                        <h4 className="font-bold text-foreground mb-1">{t('projects.projects_view.about_title')}</h4>
                        <p className="text-sm text-muted-foreground leading-relaxed">
                            {t('projects.projects_view.about_description')}
                        </p>
                    </div>
                </div>
            </div>
        </div>
    );
}
