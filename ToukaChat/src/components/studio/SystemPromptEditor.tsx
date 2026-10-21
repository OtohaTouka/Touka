"use client";

import { Settings2 } from "lucide-react";
import { cn } from "@/lib/utils";

interface SystemPromptEditorProps {
    value: string;
    onChange: (val: string) => void;
    title: string;
    readOnly?: boolean;
}

export function SystemPromptEditor({ value, onChange, title, readOnly = false }: SystemPromptEditorProps) {
    return (
        <div className={cn("space-y-2", readOnly && "opacity-90")}>
            <div className="flex items-center justify-between">
                <label className="text-xs font-black uppercase text-primary tracking-widest flex items-center gap-2">
                    <Settings2 className="w-3 h-3" /> {title}
                </label>
                <span className="text-xs text-muted-foreground font-mono bg-muted px-2 py-0.5 rounded">
                    {readOnly ? "Read Only" : "Markdown Supported"}
                </span>
            </div>
            <div className={cn(
                "relative group rounded-2xl border shadow-2xl overflow-hidden bg-background/50 backdrop-blur-sm transition-all",
                readOnly ? "border-border/50" : "border-border focus-within:border-primary/50"
            )}>
                <div className="absolute left-0 top-0 bottom-0 w-10 bg-muted/30 border-r border-border/50 flex flex-col items-center py-4 text-xs font-mono text-muted-foreground select-none">
                    {Array.from({ length: 15 }).map((_, i) => (
                        <div key={i} className="h-6 leading-6">{i + 1}</div>
                    ))}
                </div>
                <textarea
                    value={value}
                    onChange={(e) => !readOnly && onChange(e.target.value)}
                    readOnly={readOnly}
                    className={cn(
                        "w-full h-96 bg-transparent pl-14 pr-6 py-4 text-sm font-mono text-foreground leading-6 focus:outline-none resize-none custom-scrollbar",
                        readOnly ? "cursor-default" : "cursor-text"
                    )}
                    spellCheck={false}
                />
            </div>
        </div>
    );
}
