import React from "react";
import { ChevronDown } from "lucide-react";
import { cn } from "@/lib/utils";

interface SectionWrapperProps {
  title: string;
  isOpen: boolean;
  toggle: () => void;
  icon: React.ReactNode;
  children: React.ReactNode;
}

export function SectionWrapper({
  title,
  isOpen,
  toggle,
  icon,
  children,
}: SectionWrapperProps) {
  return (
    <div className="flex flex-col gap-2">
      <button
        onClick={toggle}
        className="flex items-center justify-between w-full text-left group"
      >
        <div className="flex items-center gap-2">
          <span className="text-muted-foreground group-hover:text-primary transition-colors">
            {icon}
          </span>
          <span className="text-xs font-black uppercase tracking-widest text-muted-foreground">
            {title}
          </span>
        </div>
        <ChevronDown
          className={cn(
            "w-4 h-4 text-muted-foreground transition-transform duration-200",
            isOpen ? "rotate-180" : ""
          )}
        />
      </button>
      {isOpen && (
        <div className="animate-in fade-in slide-in-from-top-1 duration-200">
          {children}
        </div>
      )}
    </div>
  );
}
