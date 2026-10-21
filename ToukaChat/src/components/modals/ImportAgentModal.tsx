import { useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";

interface PendingSession {
  id: string;
  title: string;
}

interface Props {
  pendingSessions: PendingSession[];
  availableModels: { id: string; name?: string }[];
  onConfirm: (agentId: string) => void;
  onCancel: () => void;
}

export default function ImportAgentModal({ pendingSessions, availableModels, onConfirm, onCancel }: Props) {
  const { t } = useTranslation();
  const [selected, setSelected] = useState(availableModels[0]?.id ?? "");
  const [custom, setCustom] = useState("");

  const finalValue = custom.trim() || selected;

  return (
    // WCAG 2.1.2（A3）：手寫覆蓋層 → Radix Dialog（焦點陷阱/Esc/焦點歸位內建）
    <Dialog open onOpenChange={(open) => { if (!open) onCancel(); }}>
      <DialogContent
        overlayClassName="bg-black/60 backdrop-blur-sm"
        className="bg-card border border-border rounded-2xl shadow-2xl w-full max-w-md p-6 flex flex-col gap-4"
      >
        <DialogTitle className="text-lg font-semibold text-foreground">{t('modals.import_agent_modal.title')}</DialogTitle>
        <DialogDescription className="text-sm text-muted-foreground">
          {t('modals.import_agent_modal.description')}
        </DialogDescription>

        {/* 待辨識清單 */}
        <ul className="text-sm text-foreground space-y-1 max-h-40 overflow-y-auto bg-muted rounded-lg p-3">
          {pendingSessions.map(s => (
            <li key={s.id} className="truncate">・{s.title}</li>
          ))}
        </ul>

        {/* 下拉選擇器 */}
        {availableModels.length > 0 && (
          <div className="flex flex-col gap-1">
            <label className="text-xs text-muted-foreground">{t('modals.import_agent_modal.select_from_available_label')}</label>
            <select
              className="bg-muted text-foreground text-sm rounded-lg px-3 py-2 border border-input outline-none focus:ring-2 focus:ring-primary"
              value={selected}
              onChange={e => { setSelected(e.target.value); setCustom(""); }}
            >
              {availableModels.map(m => (
                <option key={m.id} value={m.id}>{m.name ?? m.id}</option>
              ))}
            </select>
          </div>
        )}

        {/* 手填 */}
        <div className="flex flex-col gap-1">
          <label className="text-xs text-muted-foreground">{t('modals.import_agent_modal.custom_agent_id_label')}</label>
          <input
            type="text"
            className="bg-muted text-foreground text-sm rounded-lg px-3 py-2 border border-input outline-none focus:ring-2 focus:ring-primary placeholder:text-muted-foreground"
            placeholder={t('modals.import_agent_modal.custom_agent_id_placeholder')}
            value={custom}
            onChange={e => setCustom(e.target.value)}
          />
        </div>

        {/* 按鈕 */}
        <div className="flex gap-2 justify-end mt-1">
          <button
            onClick={onCancel}
            className="px-4 py-2 rounded-lg text-sm text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
          >
            {t('modals.import_agent_modal.cancel_button')}
          </button>
          <button
            onClick={() => finalValue && onConfirm(finalValue)}
            disabled={!finalValue}
            className="px-4 py-2 rounded-lg text-sm font-medium bg-primary-button hover:bg-primary-button/90 disabled:opacity-40 disabled:cursor-not-allowed text-foreground transition-colors"
          >
            {t('modals.import_agent_modal.confirm_button')}
          </button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
