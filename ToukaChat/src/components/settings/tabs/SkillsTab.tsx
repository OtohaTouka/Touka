import { useState, useRef } from "react";
import { useTranslation } from "react-i18next";
import { Zap } from "lucide-react";
import { Skill, Tool, CatalogSkill } from "@/lib/types";
import { getSystemEpoch } from "@/lib/time_utils";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { MySkillsView } from "./skills/MySkillsView";
import { SkillCatalogView } from "./skills/SkillCatalogView";
import { SkillForm } from "./skills/SkillForm";
import SkillInstallModal from "@/components/modals/SkillInstallModal";
import SkillPreviewModal from "@/components/modals/SkillPreviewModal";
import { useConfirmToast } from "@/hooks/useConfirmToast";

interface Props {
    skills: Skill[];
    editingSkill: Skill | null;
    isSavingSkill: boolean;
    setEditingSkill: (skill: Skill | null) => void;
    saveSkill: (skill: Skill) => Promise<boolean>;
    deleteSkill: (id: string) => Promise<boolean>;
    exportSkill: (skill: Skill) => Promise<boolean>;
    installSkillFromUrl: (url: string) => Promise<boolean>;
    catalogItems: CatalogSkill[];
    installCatalogSkill: (catalogId: string) => Promise<boolean>;
    getSkill: (id: string) => Promise<Skill | null>;
    systemTools: Tool[];
}


export function SkillsTab({
    skills, editingSkill, isSavingSkill,
    setEditingSkill, saveSkill, deleteSkill,
    exportSkill, installSkillFromUrl, catalogItems, installCatalogSkill, getSkill, systemTools
}: Props) {
    const { t } = useTranslation();
    const { confirmToast } = useConfirmToast();
    const [isInstallModalOpen, setInstallModalOpen] = useState(false);
    // B4 技能預覽：已安裝技能 body 延後抓取（/list 不回 body）；catalog 僅 metadata。
    const [previewSkill, setPreviewSkill] = useState<Skill | null>(null);
    const [previewCatalog, setPreviewCatalog] = useState<CatalogSkill | null>(null);
    const [previewBodyLoading, setPreviewBodyLoading] = useState(false);
    // 預覽請求序號：使用者切換／關閉預覽會遞增此值，讓進行中的 getSkill 結果作廢，
    // 避免過期回應蓋掉新狀態（造成 skill 與 catalog 同時存在）。
    const previewReqRef = useRef(0);

    const handlePreviewSkill = async (skill: Skill) => {
        const reqId = ++previewReqRef.current;
        setPreviewCatalog(null);
        setPreviewSkill(skill);
        setPreviewBodyLoading(true);
        const full = await getSkill(skill.id);
        if (reqId !== previewReqRef.current) return; // 已切換／關閉 → 丟棄過期結果
        if (full) setPreviewSkill(full);
        setPreviewBodyLoading(false);
    };
    const handlePreviewCatalog = (catalogSkill: CatalogSkill) => {
        previewReqRef.current++; // 作廢任何進行中的 skill body 抓取
        setPreviewSkill(null);
        setPreviewCatalog(catalogSkill);
        setPreviewBodyLoading(false);
    };
    const closePreview = () => {
        previewReqRef.current++;
        setPreviewSkill(null);
        setPreviewCatalog(null);
        setPreviewBodyLoading(false);
    };

    const handleSaveLocal = async () => {
        if (!editingSkill) return;
        await saveSkill(editingSkill);
    };

    const handleDelete = (id: string) => {
        confirmToast(t('settings.skills_tab.delete_confirm_message'), () => deleteSkill(id), t('settings.skills_tab.delete_confirm_action'));
    };

    const handleInstallCatalog = (catalogSkill: CatalogSkill, isUpgrade: boolean) => {
        if (isUpgrade) {
            confirmToast(
                t('settings.skills_tab.upgrade_confirm_message', { name: catalogSkill.name }),
                () => installCatalogSkill(catalogSkill.id),
                t('settings.skills_tab.upgrade_confirm_action'),
            );
            return;
        }
        confirmToast(
            t('settings.skills_tab.install_confirm_message', { name: catalogSkill.name }),
            () => installCatalogSkill(catalogSkill.id),
            t('settings.skills_tab.install_confirm_action'),
        );
    };

    const handleAddNew = () => {
        setEditingSkill({ 
            id: `skill_${getSystemEpoch()}`, 
            name: "", 
            description: "", 
            system_prompt: "", 
            required_tools: [], 
            mcp_servers: [] 
        });
    };

    return (
        <div className="space-y-6 animate-in fade-in slide-in-from-bottom-2 duration-300">
            <section className="space-y-4">
                <h4 className="text-sm font-bold text-foreground flex items-center gap-2">
                    <Zap className="w-4 h-4 text-primary" /> {t('settings.skills_tab.heading')}
                </h4>
                <p className="text-xs text-muted-foreground max-w-2xl leading-relaxed">
                    {t('settings.skills_tab.description')}
                </p>
            </section>

            {editingSkill ? (
                <SkillForm
                    editingSkill={editingSkill}
                    setEditingSkill={setEditingSkill}
                    isSavingSkill={isSavingSkill}
                    onSave={handleSaveLocal}
                    systemTools={systemTools}
                />
            ) : (
                <Tabs defaultValue="my_skills" className="w-full">
                    <TabsList className="bg-muted/30 p-1 rounded-2xl border border-border h-auto gap-1 mb-6">
                        <TabsTrigger 
                            value="my_skills"
                            className="px-6 py-2 rounded-xl text-xs font-bold data-[state=active]:bg-primary data-[state=active]:text-white shadow-none"
                        >
                            {t('settings.skills_tab.my_skills_tab', { count: skills.length })}
                        </TabsTrigger>
                        <TabsTrigger
                            value="real_catalog"
                            className="px-6 py-2 rounded-xl text-xs font-bold data-[state=active]:bg-primary data-[state=active]:text-white shadow-none"
                        >
                            {t('settings.skills_tab.skill_hub_tab')}
                        </TabsTrigger>
                    </TabsList>

                    <TabsContent value="my_skills" className="m-0 focus-visible:outline-none">
                        <MySkillsView
                            skills={skills}
                            onEdit={setEditingSkill}
                            onDelete={handleDelete}
                            onExport={exportSkill}
                            onAddNew={handleAddNew}
                            onInstallFromUrl={() => setInstallModalOpen(true)}
                            onPreview={handlePreviewSkill}
                        />
                    </TabsContent>

                    <TabsContent value="real_catalog" className="m-0 focus-visible:outline-none">
                        <SkillCatalogView
                            skills={skills}
                            catalogItems={catalogItems}
                            onInstall={handleInstallCatalog}
                            onPreview={handlePreviewCatalog}
                        />
                    </TabsContent>
                </Tabs>
            )}

            {isInstallModalOpen && (
                <SkillInstallModal
                    onConfirm={installSkillFromUrl}
                    onCancel={() => setInstallModalOpen(false)}
                />
            )}

            {(previewSkill || previewCatalog) && (
                <SkillPreviewModal
                    skill={previewSkill}
                    catalogSkill={previewCatalog}
                    bodyLoading={previewBodyLoading}
                    onClose={closePreview}
                />
            )}
        </div>
    );
}

