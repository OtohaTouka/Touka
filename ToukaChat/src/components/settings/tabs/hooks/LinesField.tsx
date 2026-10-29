import { useState } from "react";
import { Textarea } from "@/components/ui/textarea";

interface LinesFieldProps<T> {
    value: T;
    format: (value: T) => string;
    parse: (text: string) => T;
    onChange: (value: T) => void;
    label: string;
    placeholder?: string;
}

/**
 * 一行一項的文字欄位：打字時保留原文（含還沒打完的行），只有外部值和目前文字解析結果不同時才換掉文字。
 */
export function LinesField<T>({ value, format, parse, onChange, label, placeholder }: LinesFieldProps<T>) {
    const [text, setText] = useState(() => format(value));
    const shown = format(parse(text)) === format(value) ? text : format(value);
    return (
        <Textarea
            value={shown}
            onChange={(e) => {
                setText(e.target.value);
                onChange(parse(e.target.value));
            }}
            aria-label={label}
            placeholder={placeholder}
            spellCheck={false}
            className="font-mono text-xs"
        />
    );
}
