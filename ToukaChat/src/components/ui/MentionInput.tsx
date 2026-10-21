"use client";

import React, { useState, useRef } from "react";
import { useTranslation } from "react-i18next";
import { Textarea } from "./textarea";
import { FileCode2, Terminal, Globe, Search, Command } from "lucide-react";
import { cn } from "@/lib/utils";
import { useEditorFiles } from "@/hooks/useEditorFiles";

interface MentionInputProps extends React.TextareaHTMLAttributes<HTMLTextAreaElement> {
  onSend?: (value: string) => void;
}

interface MentionItem {
  name: string;
  path: string;
  type: string;
  icon?: React.ReactNode;
  desc?: string;
  descKey?: string;
}

const specialContexts: MentionItem[] = [
  { name: "codebase", path: "codebase", type: "special", icon: <Globe size={14} className="text-primary" />, descKey: "ui.mention_input.context.codebase_desc" },
  { name: "terminal", path: "terminal", type: "special", icon: <Terminal size={14} className="text-primary" />, descKey: "ui.mention_input.context.terminal_desc" },
  { name: "git", path: "git", type: "special", icon: <Search size={14} className="text-primary" />, descKey: "ui.mention_input.context.git_desc" },
];

export function MentionInput({ value, onChange, onKeyDown, className, ...props }: MentionInputProps) {
  const { t } = useTranslation();
  const [showMentions, setShowMentions] = useState(false);
  const [mentionQuery, setMentionQuery] = useState("");
  const [selectedIndex, setSelectedIndex] = useState(0);
  const { files: fileIndex } = useEditorFiles();
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);


  const filteredItems: MentionItem[] = [
    ...specialContexts.filter(c => c.name.includes(mentionQuery.toLowerCase())),
    ...fileIndex.filter(f => f.path.toLowerCase().includes(mentionQuery.toLowerCase())).slice(0, 10)
  ];

  const handleInputChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const val = e.target.value;
    if (onChange) onChange(e);

    const selectionStart = e.target.selectionStart;
    const textBeforeCursor = val.slice(0, selectionStart);
    const mentionMatch = textBeforeCursor.match(/@(\S*)$/);

    if (mentionMatch) {
      setShowMentions(true);
      setMentionQuery(mentionMatch[1]);
      setSelectedIndex(0);
      
      // 計算彈出視窗位置 (簡化版：僅放在上方)
      // 在實際生產環境中，可能需要精確計算游標座標
    } else {
      setShowMentions(false);
    }
  };

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const insertMention = (item: any) => {
    if (!textareaRef.current) return;
    const val = String(value || "");
    const selectionStart = textareaRef.current.selectionStart;
    const textBeforeCursor = val.slice(0, selectionStart);
    const textAfterCursor = val.slice(selectionStart);
    
    const newTextBefore = textBeforeCursor.replace(/@(\S*)$/, `@${item.path} `);
    const newValue = newTextBefore + textAfterCursor;

    // 模擬 Event 調用 onChange
    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value")?.set;
    nativeInputValueSetter?.call(textareaRef.current, newValue);
    const event = new Event('input', { bubbles: true });
    textareaRef.current.dispatchEvent(event);

    setShowMentions(false);
    textareaRef.current.focus();
  };

  const handleCustomKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (showMentions) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSelectedIndex(prev => (prev + 1) % filteredItems.length);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setSelectedIndex(prev => (prev - 1 + filteredItems.length) % filteredItems.length);
      } else if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        if (filteredItems[selectedIndex]) {
          insertMention(filteredItems[selectedIndex]);
        }
      } else if (e.key === "Escape") {
        setShowMentions(false);
      }
    } else {
      if (onKeyDown) onKeyDown(e);
    }
  };

  return (
    <div className="relative w-full" ref={containerRef}>
      {showMentions && filteredItems.length > 0 && (
        <div 
          className="absolute bottom-full left-0 mb-2 w-full max-w-md bg-popover border border-border rounded-xl shadow-2xl z-[500] overflow-hidden animate-in fade-in slide-in-from-bottom-2 duration-200"
        >
          <div className="p-2 border-b border-border bg-muted flex items-center gap-2">
            <Command size={12} className="text-muted-foreground" />
            <span className="text-xs font-black uppercase tracking-widest text-muted-foreground">Context Mentions</span>
          </div>
          <div className="max-h-60 overflow-y-auto p-1 custom-scrollbar">
            {filteredItems.map((item, idx) => (
              <div
                key={item.path}
                onClick={() => insertMention(item)}
                className={cn(
                  "flex items-center gap-3 px-3 py-2 rounded-lg cursor-pointer transition-colors",
                  idx === selectedIndex ? "bg-primary-button text-white" : "hover:bg-accent text-foreground"
                )}
              >
                <div className={cn("shrink-0", idx === selectedIndex ? "text-white" : "text-muted-foreground")}>
                  {item.type === "special" ? item.icon : <FileCode2 size={14} />}
                </div>
                <div className="flex flex-col min-w-16 overflow-hidden">
                  <span className="text-xs font-bold truncate">{item.name}</span>
                  <span className={cn("text-xs truncate opacity-70", idx === selectedIndex ? "text-white" : "text-muted-foreground")}>
                    {(item.descKey ? t(item.descKey) : item.desc) || item.path}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
      <Textarea
        {...props}
        ref={textareaRef}
        value={value}
        onChange={handleInputChange}
        onKeyDown={handleCustomKeyDown}
        className={className}
      />
    </div>
  );
}
