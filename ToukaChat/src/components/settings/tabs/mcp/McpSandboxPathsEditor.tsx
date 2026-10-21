import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Plus, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { SandboxPathGrant } from "@/lib/types";

// 每台最多幾筆（後端 models/mcp_isolation.MAX_SANDBOX_PATHS）
const MAX_SANDBOX_PATHS = 16;
const ACCESS_MODES: SandboxPathGrant["access"][] = ["ro", "rw"];

interface Props {
  saved: SandboxPathGrant[];
  saving: boolean;
  onSave: (paths: SandboxPathGrant[]) => Promise<boolean>;
}

/** 第三方 MCP 的額外允許資料夾；存檔時後端逐筆檢查，Touka 的資料夾一律不給。 */
export function McpSandboxPathsEditor({ saved, saving, onSave }: Props) {
  const { t } = useTranslation();
  const [rows, setRows] = useState<SandboxPathGrant[]>(saved);
  // 有空白列就不能存：後端回的「第幾筆」要和畫面上的列一致
  const incomplete = rows.some((row) => !row.path.trim());

  const setRow = (index: number, patch: Partial<SandboxPathGrant>) =>
    setRows((prev) =>
      prev.map((row, i) => (i === index ? { ...row, ...patch } : row)),
    );

  const handleSave = () =>
    void onSave(rows.map((row) => ({ ...row, path: row.path.trim() })));

  return (
    <div className="space-y-2">
      <Label className="text-muted-foreground text-xs font-bold">
        {t("settings.mcp_isolation.paths_title")}
      </Label>
      <p className="text-muted-foreground text-xs">
        {t("settings.mcp_isolation.paths_hint")}
      </p>
      {rows.map((row, index) => (
        <div key={index} className="flex items-center gap-2">
          <Input
            aria-label={t("settings.mcp_isolation.path_aria", { n: index + 1 })}
            placeholder={t("settings.mcp_isolation.path_ph")}
            value={row.path}
            onChange={(e) => setRow(index, { path: e.target.value })}
            className="bg-background border-border h-9 rounded-xl font-mono text-xs"
          />
          {ACCESS_MODES.map((access) => (
            <Button
              key={access}
              type="button"
              size="sm"
              variant={row.access === access ? "default" : "outline"}
              onClick={() => setRow(index, { access })}
              className="rounded-xl text-xs font-bold"
            >
              {t(`settings.mcp_isolation.access_${access}`)}
            </Button>
          ))}
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label={t("settings.mcp_isolation.remove_path_aria", {
              n: index + 1,
            })}
            onClick={() =>
              setRows((prev) => prev.filter((_, i) => i !== index))
            }
            className="text-muted-foreground hover:text-destructive h-8 w-8 rounded-lg"
          >
            <X className="h-4 w-4" />
          </Button>
        </div>
      ))}
      <div className="flex items-center gap-2">
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={rows.length >= MAX_SANDBOX_PATHS}
          onClick={() =>
            setRows((prev) => [...prev, { path: "", access: "ro" }])
          }
          className="rounded-xl text-xs font-bold"
        >
          <Plus className="h-3 w-3" /> {t("settings.mcp_isolation.add_path")}
        </Button>
        <Button
          type="button"
          size="sm"
          disabled={saving || incomplete}
          onClick={handleSave}
          className="rounded-xl text-xs font-bold"
        >
          {t("settings.mcp_isolation.save_paths")}
        </Button>
      </div>
    </div>
  );
}
