import { useTranslation } from "react-i18next";
import { Trash2 } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Button } from "@/components/ui/button";
import type { HookHandler } from "@/lib/types";
import { LinesField } from "./LinesField";
import {
    MAX_HOOK_TIMEOUT_S,
    argsToText,
    headersToText,
    textToArgs,
    textToHeaders,
} from "./hook_draft";

interface HookHandlerEditorProps {
    hook: HookHandler;
    onChange: (hook: HookHandler) => void;
    onRemove: () => void;
}

/** 一個 hook 的欄位：名稱、啟用、逾時，加上程式＋參數或網址＋標頭。 */
export function HookHandlerEditor({ hook, onChange, onRemove }: HookHandlerEditorProps) {
    const { t } = useTranslation();
    const field = (key: string) => t(`settings.hooks_tab.fields.${key}`);
    return (
        <div className="space-y-3 rounded-md border border-border p-3">
            <div className="flex items-center gap-2">
                <span className="rounded bg-muted px-2 py-0.5 font-mono text-xs text-muted-foreground">
                    {t(`settings.hooks_tab.types.${hook.type}`)}
                </span>
                <Input
                    value={hook.name ?? ""}
                    onChange={(e) => onChange({ ...hook, name: e.target.value || null })}
                    aria-label={field("name")}
                    placeholder={field("name")}
                    className="h-8 flex-1"
                />
                <Switch
                    checked={hook.enabled}
                    onCheckedChange={(enabled) => onChange({ ...hook, enabled })}
                    aria-label={field("enabled")}
                />
                <Button variant="ghost" size="icon" onClick={onRemove} aria-label={t("settings.hooks_tab.remove_hook")}>
                    <Trash2 className="h-4 w-4" />
                </Button>
            </div>
            {hook.type === "command" ? (
                <>
                    <Input
                        value={hook.command}
                        onChange={(e) => onChange({ ...hook, command: e.target.value })}
                        aria-label={field("command")}
                        placeholder={field("command_placeholder")}
                        spellCheck={false}
                        className="h-8 font-mono text-xs"
                    />
                    <LinesField
                        value={hook.args ?? []}
                        format={argsToText}
                        parse={textToArgs}
                        onChange={(args) => onChange({ ...hook, args })}
                        label={field("args")}
                        placeholder={field("args_placeholder")}
                    />
                </>
            ) : (
                <>
                    <Input
                        value={hook.url}
                        onChange={(e) => onChange({ ...hook, url: e.target.value })}
                        aria-label={field("url")}
                        placeholder={field("url_placeholder")}
                        spellCheck={false}
                        className="h-8 font-mono text-xs"
                    />
                    <LinesField
                        value={hook.headers ?? {}}
                        format={headersToText}
                        parse={textToHeaders}
                        onChange={(headers) => onChange({ ...hook, headers })}
                        label={field("headers")}
                        placeholder={field("headers_placeholder")}
                    />
                </>
            )}
            <label className="flex items-center gap-2 text-xs text-muted-foreground">
                {field("timeout")}
                <Input
                    type="number"
                    min={1}
                    max={MAX_HOOK_TIMEOUT_S}
                    value={hook.timeout}
                    onChange={(e) => onChange({ ...hook, timeout: Number(e.target.value) })}
                    className="h-8 w-24"
                />
            </label>
        </div>
    );
}
