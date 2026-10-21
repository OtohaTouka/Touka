import { useState, useCallback } from 'react';
import { Skill, CatalogSkill } from '@/lib/types';
import { getActiveProxyUrl, fetchTouka } from '@/lib/connections';
import { logSSOTError, errorMsg } from '@/lib/error_handler';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';

export function useSkillSettings() {
    const { t } = useTranslation();
    const [skills, setSkills] = useState<Skill[]>([]);
    const [catalogItems, setCatalogItems] = useState<CatalogSkill[]>([]);
    const [editingSkill, setEditingSkill] = useState<Skill | null>(null);
    const [isSavingSkill, setIsSavingSkill] = useState(false);

    const fetchSkills = useCallback(async () => {
        const proxyUrl = getActiveProxyUrl();
        try {
            const sRes = await fetchTouka(proxyUrl, "/v1/skills/list");
            if (sRes.ok) setSkills((await sRes.json()).skills || []);
        } catch (e) {
            logSSOTError(e, "useSkillSettings:fetchSkills");
        }
    }, []);

    // 取單一已安裝技能的完整契約（含 system_prompt body；/list 不回 body）。供預覽用。
    const getSkill = useCallback(async (skillId: string): Promise<Skill | null> => {
        const proxyUrl = getActiveProxyUrl();
        try {
            const res = await fetchTouka(proxyUrl, `/v1/skills/${skillId}`);
            if (res.ok) return (await res.json()) as Skill;
        } catch (e) {
            logSSOTError(e, "useSkillSettings:getSkill");
        }
        return null;
    }, []);

    // ADR-0074：讀取真技能策展目錄（能力型 .skill）。
    const fetchSkillCatalog = useCallback(async () => {
        const proxyUrl = getActiveProxyUrl();
        try {
            const res = await fetchTouka(proxyUrl, "/v1/skills/catalog");
            if (res.ok) setCatalogItems((await res.json()).catalog || []);
        } catch (e) {
            logSSOTError(e, "useSkillSettings:fetchSkillCatalog");
        }
    }, []);

    const saveSkill = async (skill: Skill) => {
        setIsSavingSkill(true);
        const proxyUrl = getActiveProxyUrl();
        try {
            const res = await fetchTouka(proxyUrl, "/v1/skills", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(skill)
            });
            if (res.ok) {
                await fetchSkills();
                setEditingSkill(null);
                return true;
            }
        } catch (e) {
            logSSOTError(e, "useSkillSettings:saveSkill");
        } finally {
            setIsSavingSkill(false);
        }
        return false;
    };

    const deleteSkill = async (skillId: string) => {
        const proxyUrl = getActiveProxyUrl();
        try {
            const res = await fetchTouka(proxyUrl, `/v1/skills/${skillId}`, {
                method: "DELETE"
            });
            if (res.ok) {
                await fetchSkills();
                return true;
            }
        } catch (e) {
            logSSOTError(e, "useSkillSettings:deleteSkill");
        }
        return false;
    };

    const exportSkill = async (skill: Skill) => {
        const proxyUrl = getActiveProxyUrl();
        try {
            const res = await fetchTouka(proxyUrl, `/v1/skills/${skill.id}/export`);
            if (res.ok) {
                const blob = await res.blob();
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `${skill.id}.skill`;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(url);
                return true;
            }
            toast.error(errorMsg("SYSTEM_ERROR"));
        } catch (e) {
            logSSOTError(e, "useSkillSettings:exportSkill");
            toast.error(errorMsg("SYSTEM_ERROR"));
        }
        return false;
    };

    const installSkillFromUrl = async (url: string) => {
        const proxyUrl = getActiveProxyUrl();
        try {
            const res = await fetchTouka(proxyUrl, "/v1/skills/install", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ url })
            });
            if (res.ok) {
                await fetchSkills();
                toast.success(t('hooks.use_skill_settings.install_success'));
                return true;
            }
            toast.error(errorMsg("CONFIG_SAVE_FAILED"));
        } catch (e) {
            logSSOTError(e, "useSkillSettings:installSkillFromUrl");
            toast.error(errorMsg("SYSTEM_ERROR"));
        }
        return false;
    };

    // ADR-0074：安裝策展真技能（含可執行腳本；後端只認 catalog 內已審 id，逐檔釘 commit raw 抓取）。
    const installCatalogSkill = async (catalogId: string) => {
        const proxyUrl = getActiveProxyUrl();
        try {
            const res = await fetchTouka(proxyUrl, "/v1/skills/catalog/install", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ catalog_id: catalogId })
            });
            if (res.ok) {
                await fetchSkills();
                toast.success(t('hooks.use_skill_settings.install_success'));
                return true;
            }
            toast.error(errorMsg("CONFIG_SAVE_FAILED"));
        } catch (e) {
            logSSOTError(e, "useSkillSettings:installCatalogSkill");
            toast.error(errorMsg("SYSTEM_ERROR"));
        }
        return false;
    };

    return {
        skills, catalogItems, editingSkill, isSavingSkill,
        setEditingSkill,
        fetchSkills, fetchSkillCatalog, saveSkill, deleteSkill,
        exportSkill, installSkillFromUrl, installCatalogSkill, getSkill
    };
}
