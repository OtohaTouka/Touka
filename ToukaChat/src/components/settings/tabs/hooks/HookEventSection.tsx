import { useTranslation } from "react-i18next";
import { Plus, Trash2 } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import type { HookHandler, HookMatcherGroup } from "@/lib/types";
import { HookHandlerEditor } from "./HookHandlerEditor";
import { newGroup, newHook, removeAt, replaceAt } from "./hook_draft";

interface HookEventSectionProps {
    event: string;
    usesMatcher: boolean;
    groups: HookMatcherGroup[];
    onChange: (groups: HookMatcherGroup[]) => void;
}

const HOOK_TYPES: HookHandler["type"][] = ["command", "http"];

/** 一個事件的設定：每組一個比對規則（只有看工具名稱的事件才有）和它的 hook。 */
export function HookEventSection({ event, usesMatcher, groups, onChange }: HookEventSectionProps) {
    const { t } = useTranslation();
    const setGroup = (index: number, group: HookMatcherGroup) => onChange(replaceAt(groups, index, group));
    const setHooks = (index: number, hooks: HookHandler[]) =>
        hooks.length > 0 ? setGroup(index, { ...groups[index], hooks }) : onChange(removeAt(groups, index));

    return (
        <section className="space-y-3 rounded-lg border border-border p-4">
            <div className="space-y-0.5">
                <h5 className="font-mono text-sm font-semibold text-foreground">{event}</h5>
                <p className="text-xs text-muted-foreground">{t(`settings.hooks_tab.events.${event}`)}</p>
            </div>
            {groups.map((group, index) => (
                <div key={index} className="space-y-2 rounded-md bg-muted/30 p-3">
                    <div className="flex items-center gap-2">
                        {usesMatcher && (
                            <Input
                                value={group.matcher ?? ""}
                                onChange={(e) => setGroup(index, { ...group, matcher: e.target.value })}
                                aria-label={t("settings.hooks_tab.fields.matcher")}
                                placeholder={t("settings.hooks_tab.fields.matcher_placeholder")}
                                spellCheck={false}
                                className="h-8 flex-1 font-mono text-xs"
                            />
                        )}
                        <div className="ml-auto flex gap-1">
                            {HOOK_TYPES.map((type) => (
                                <Button
                                    key={type}
                                    variant="outline"
                                    size="sm"
                                    onClick={() => setHooks(index, [...group.hooks, newHook(type)])}
                                >
                                    <Plus className="h-3.5 w-3.5" /> {t(`settings.hooks_tab.types.${type}`)}
                                </Button>
                            ))}
                            <Button
                                variant="ghost"
                                size="icon"
                                onClick={() => onChange(removeAt(groups, index))}
                                aria-label={t("settings.hooks_tab.remove_group")}
                            >
                                <Trash2 className="h-4 w-4" />
                            </Button>
                        </div>
                    </div>
                    {group.hooks.map((hook, hookIndex) => (
                        <HookHandlerEditor
                            key={hookIndex}
                            hook={hook}
                            onChange={(next) => setHooks(index, replaceAt(group.hooks, hookIndex, next))}
                            onRemove={() => setHooks(index, removeAt(group.hooks, hookIndex))}
                        />
                    ))}
                </div>
            ))}
            <Button variant="outline" size="sm" onClick={() => onChange([...groups, newGroup("command")])}>
                <Plus className="h-3.5 w-3.5" /> {t("settings.hooks_tab.add_group")}
            </Button>
        </section>
    );
}
