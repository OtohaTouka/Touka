"use client";

import { useEffect, useState } from "react";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

import { Separator } from "@/components/ui/separator";
import { TagInput } from "@/components/ui/tag-input";

interface DynamicToolFormProps {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- 動態工具 JSON schema（任意工具定義，結構由後端決定）
  schema: any;
  onChange: (values: Record<string, unknown>) => void;
}

export default function DynamicToolForm({ schema, onChange }: DynamicToolFormProps) {
  const [values, setValues] = useState<Record<string, unknown>>({});

  const properties = schema?.function?.parameters?.properties || {};
  const required = schema?.function?.parameters?.required || [];

  // Reset values when schema changes
  useEffect(() => {
    const initialValues: Record<string, unknown> = {};
    Object.keys(properties).forEach((key) => {
      const prop = properties[key];
      if (prop.default !== undefined) {
        initialValues[key] = prop.default;
      } else if (prop.type === "boolean") {
        initialValues[key] = false;
      } else {
        initialValues[key] = "";
      }
    });
    // eslint-disable-next-line react-hooks/set-state-in-effect -- schema 變更時重置表單預設並回拋父層
    setValues(initialValues);
    onChange(initialValues);
  }, [schema, onChange]);

  const handleFieldChange = (key: string, value: unknown) => {
    setValues(prev => {
      const updated = { ...prev, [key]: value };
      onChange(updated);
      return updated;
    });
  };

  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- prop 為動態 JSON schema 屬性定義
  const renderField = (key: string, prop: any) => {
    const isRequired = required.includes(key);

    if (prop.enum) {
      return (
        <div key={key} className="space-y-2">
          <Label className="text-xs font-semibold text-foreground/80 flex items-center justify-between gap-3 min-w-0">
            <span className="break-words" title={key}>{key}</span>
            {isRequired && <span className="text-destructive text-xs font-bold shrink-0">REQUIRED</span>}
          </Label>
          <Select
            value={values[key] as string}
            onValueChange={(val) => handleFieldChange(key, val)}
          >
            <SelectTrigger className="h-9 text-xs bg-muted/30 focus:ring-1 focus:ring-primary/40">
              <SelectValue placeholder={`Select ${key}...`} />
            </SelectTrigger>
            <SelectContent>
              {prop.enum.map((opt: string) => (
                <SelectItem key={opt} value={opt} className="text-xs">
                  {opt}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {prop.description && (
            <p className="text-xs text-muted-foreground leading-tight italic px-1 break-words">
              {prop.description}
            </p>
          )}
        </div>
      );
    }

    if (prop.type === "boolean") {
      return (
        <div key={key} className="flex items-center space-x-3 py-2 px-1 rounded-md hover:bg-muted/30 transition-colors">
          <Checkbox
            id={key}
            checked={!!values[key]}
            onCheckedChange={(checked) => handleFieldChange(key, checked)}
          />
          <div className="space-y-0.5">
            <Label htmlFor={key} className="text-xs font-medium cursor-pointer flex items-center gap-2">
              {key}
              {isRequired && <span className="text-destructive font-bold text-xs">REQUIRED</span>}
            </Label>
            {prop.description && (
            <p className="text-xs text-muted-foreground leading-tight break-words">{prop.description}</p>
            )}
          </div>
        </div>
      );
    }

    if (prop.type === "object" || prop.type === "array") {
      const isStringArray = prop.type === "array" && prop.items?.type === "string";

      return (
        <div key={key} className="space-y-2">
          <Label className="text-xs font-semibold text-foreground/80 flex items-center justify-between gap-3 min-w-0">
            <span className="break-words" title={key}>
              {key}{" "}
              <span className="text-xs font-normal text-muted-foreground ml-1 shrink-0">
                ({isStringArray ? "Tags" : `${prop.type} JSON`})
              </span>
            </span>
            {isRequired && <span className="text-destructive text-xs font-bold shrink-0">REQUIRED</span>}
          </Label>

          {isStringArray ? (
            <TagInput
              tags={(values[key] as string[]) || []}
              onChange={(tags) => handleFieldChange(key, tags)}
              placeholder={`Add ${key}...`}
            />
          ) : (
            <Textarea
              placeholder={`Enter ${prop.type} JSON...`}
              value={typeof values[key] === "string" ? (values[key] as string) : JSON.stringify(values[key], null, 2)}
              onChange={(e) => {
                const val = e.target.value;
                try {
                  const parsed = JSON.parse(val);
                  handleFieldChange(key, parsed);
                } catch {
                  handleFieldChange(key, val);
                }
              }}
              className="text-xs font-mono min-h-20 bg-muted/30 focus-visible:ring-primary/40 resize-y"
            />
          )}

          {prop.description && (
            <p className="text-xs text-muted-foreground leading-tight px-1 break-words">{prop.description}</p>
          )}
        </div>
      );
    }

    return (
      <div key={key} className="space-y-2">
        <Label className="text-xs font-semibold text-foreground/80 flex items-center justify-between gap-3 min-w-0">
          <span className="break-words" title={key}>{key}</span>
          {isRequired && <span className="text-destructive text-xs font-bold shrink-0">REQUIRED</span>}
        </Label>
        <Input
          type={prop.type === "integer" || prop.type === "number" ? "number" : "text"}
          placeholder={prop.description || ""}
          value={(values[key] as string | number) || ""}
          onChange={(e) => {
            const val = prop.type === "integer" || prop.type === "number" ? Number(e.target.value) : e.target.value;
            handleFieldChange(key, val);
          }}
          className="h-9 text-xs bg-muted/30 focus-visible:ring-primary/40"
        />
        {(prop.description && !prop.enum) && (
          <p className="text-xs text-muted-foreground leading-tight px-1 break-words">{prop.description}</p>
        )}
      </div>
    );
  };

  return (
    <div className="space-y-6 pt-1 w-full min-w-0">
      {Object.keys(properties).map((key, index) => (
        <div key={key} className="w-full min-w-0 overflow-hidden">
          {index > 0 && <Separator className="opacity-40 mb-6" />}
          {renderField(key, properties[key])}
        </div>
      ))}
    </div>
  );
}
