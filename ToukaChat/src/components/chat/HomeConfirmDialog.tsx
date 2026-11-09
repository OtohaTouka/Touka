import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import type { HomeRequest } from "@/hooks/useHomeConfirm";

interface HomeConfirmDialogProps {
  request: HomeRequest | null;
  onCancel: () => void;
  onConfirm: () => void;
}

export function HomeConfirmDialog({ request, onCancel, onConfirm }: HomeConfirmDialogProps) {
  const { t } = useTranslation();
  const unsetting = request?.setting === false;
  return (
    <Dialog open={request !== null} onOpenChange={(open) => { if (!open) onCancel(); }}>
      <DialogContent
        overlayClassName="bg-background/60 backdrop-blur-md"
        className="bg-card border-border w-full max-w-md space-y-5 rounded-3xl border p-8 shadow-2xl"
      >
        <DialogTitle className="text-foreground text-lg font-black">
          {unsetting ? t("chat.home_confirm.unset_title") : t("chat.home_confirm.set_title")}
        </DialogTitle>
        <DialogDescription className="text-foreground/80 text-sm leading-relaxed">
          {unsetting ? t("chat.home_confirm.unset_body") : t("chat.home_confirm.set_body")}
        </DialogDescription>
        <div className="grid grid-cols-2 gap-3 pt-1">
          <Button variant="outline" onClick={onCancel} className="h-10 rounded-xl text-xs font-bold">
            {t("chat.home_confirm.keep")}
          </Button>
          <Button onClick={onConfirm} className="h-10 rounded-xl text-xs font-bold">
            {unsetting ? t("chat.home_confirm.confirm_unset") : t("chat.home_confirm.confirm_set")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
