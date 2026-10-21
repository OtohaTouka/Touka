"use client";

import { useState, KeyboardEvent } from "react";
import { useTranslation } from "react-i18next";
import { X } from "lucide-react";
import { Badge } from "./badge";

interface TagInputProps {
  placeholder?: string;
  tags: string[];
  onChange: (tags: string[]) => void;
  className?: string;
}

export function TagInput({
  placeholder = "Add tag...",
  tags = [],
  onChange,
  className = "",
}: TagInputProps) {
  const { t } = useTranslation();
  const [inputValue, setInputValue] = useState("");

  const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      addTag();
    } else if (e.key === "Backspace" && !inputValue && tags.length > 0) {
      removeTag(tags.length - 1);
    }
  };

  const addTag = () => {
    const trimmed = inputValue.trim().replace(/,$/, "");
    if (trimmed && !tags.includes(trimmed)) {
      const newTags = [...tags, trimmed];
      onChange(newTags);
      setInputValue("");
    } else if (tags.includes(trimmed)) {
      setInputValue("");
    }
  };

  const removeTag = (index: number) => {
    const newTags = tags.filter((_, i) => i !== index);
    onChange(newTags);
  };

  return (
    <div className={`flex flex-col gap-2 ${className}`}>
      <div className="flex flex-wrap gap-1.5 p-1 h-11 border rounded-md bg-muted/20 focus-within:ring-1 focus-within:ring-primary/40 transition-all">
        {tags.map((tag, index) => (
          <Badge
            key={`${tag}-${index}`}
            variant="secondary"
            className="flex items-center gap-1.5 px-2 py-0.5 text-xs bg-primary/20 hover:bg-primary/30 text-primary-foreground border-none transition-colors"
          >
            {tag}
            <button
              type="button"
              aria-label={t("ui.tag_input.remove_tag_aria", { tag })}
              onClick={() => removeTag(index)}
              className="hover:text-destructive transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-primary rounded-sm"
            >
              <X size={10} />
            </button>
          </Badge>
        ))}
        <input
          type="text"
          value={inputValue}
          onChange={(e) => setInputValue(e.target.value)}
          onKeyDown={handleKeyDown}
          onBlur={addTag}
          placeholder={tags.length === 0 ? placeholder : ""}
          className="flex-1 bg-transparent border-none outline-none text-xs min-w-16 p-1 placeholder:text-muted-foreground"
        />
      </div>
      <p className="text-xs text-muted-foreground/80 italic px-1">
        Press <span className="font-mono bg-muted px-1 rounded">Enter</span> or <span className="font-mono bg-muted px-1 rounded">,</span> to add.
      </p>
    </div>
  );
}
