import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Download } from "lucide-react";
import {
    Dialog,
    DialogContent,
    DialogTitle,
    DialogDescription,
} from "@/components/ui/dialog";

interface Props {
    onConfirm: (url: string) => Promise<boolean>;
    onCancel: () => void;
}

export default function SkillInstallModal({ onConfirm, onCancel }: Props) {
    const { t } = useTranslation();
    const [url, setUrl] = useState("");
    const [isInstalling, setIsInstalling] = useState(false);

    const handleInstall = async () => {
        if (!url.trim()) return;
        setIsInstalling(true);
        const ok = await onConfirm(url.trim());
        setIsInstalling(false);
        if (ok) onCancel();
    };

    return (
        // WCAG 2.1.2（P3）：手寫覆蓋層 → Radix Dialog（焦點陷阱/Esc/焦點歸位/aria-modal 內建）
        <Dialog open onOpenChange={(open) => { if (!open) onCancel(); }}>
            <DialogContent
                overlayClassName="bg-black/60 backdrop-blur-sm"
                className="bg-card border border-border rounded-2xl shadow-2xl w-full max-w-md p-6 flex flex-col gap-4"
            >
                <DialogTitle className="text-lg font-semibold text-foreground flex items-center gap-2">
                    <Download size={18} /> {t("modals.skill_install_modal.title")}
                </DialogTitle>
                <DialogDescription className="text-sm text-muted-foreground">
                    {t("modals.skill_install_modal.trusted_source_hint")}
                </DialogDescription>
                <div className="flex flex-col gap-1">
                    <label className="text-xs text-muted-foreground">{t("modals.skill_install_modal.url_label")}</label>
                    <input
                        type="url"
                        className="bg-muted text-foreground text-sm rounded-lg px-3 py-2 border border-input outline-none focus:ring-2 focus:ring-primary placeholder:text-muted-foreground"
                        placeholder="https://github.com/.../my-skill.skill"
                        value={url}
                        onChange={e => setUrl(e.target.value)}
                        disabled={isInstalling}
                    />
                </div>
                <div className="flex gap-2 justify-end mt-1">
                    <button
                        onClick={onCancel}
                        disabled={isInstalling}
                        className="px-4 py-2 rounded-lg text-sm text-muted-foreground hover:text-foreground hover:bg-muted transition-colors disabled:opacity-40"
                    >
                        {t("modals.skill_install_modal.cancel")}
                    </button>
                    <button
                        onClick={handleInstall}
                        disabled={!url.trim() || isInstalling}
                        className="px-4 py-2 rounded-lg text-sm font-medium bg-primary-button hover:bg-primary-button/90 disabled:opacity-40 disabled:cursor-not-allowed text-foreground transition-colors"
                    >
                        {isInstalling ? t("modals.skill_install_modal.installing") : t("modals.skill_install_modal.install")}
                    </button>
                </div>
            </DialogContent>
        </Dialog>
    );
}
