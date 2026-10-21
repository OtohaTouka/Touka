"use client";

import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { CircleStop, Send, X } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  type McpInteractionAction,
  useMcpInteraction,
} from "@/hooks/useMcpInteraction";
import { errorMsg, getErrorMessage, logSSOTError } from "@/lib/error_handler";
import type { ToolStep } from "@/lib/tree_history";

interface SchemaProperty {
  type?: string;
  title?: string;
  description?: string;
  default?: unknown;
  enum?: unknown[];
}

type FieldValue = string | number | boolean;

function schemaProperties(
  schema: Record<string, unknown>,
): Record<string, SchemaProperty> {
  const properties = schema.properties;
  if (!properties || typeof properties !== "object" || Array.isArray(properties)) {
    return {};
  }
  return properties as Record<string, SchemaProperty>;
}

function initialValues(
  properties: Record<string, SchemaProperty>,
): Record<string, FieldValue> {
  return Object.fromEntries(
    Object.entries(properties).flatMap(([name, property]) => {
      if (property.default === undefined) return [];
      if (
        typeof property.default === "string" ||
        typeof property.default === "number" ||
        typeof property.default === "boolean"
      ) {
        return [[name, property.default as FieldValue]];
      }
      return [];
    }),
  );
}

export function McpInteractionCard({ step }: { step: ToolStep }) {
  const { t } = useTranslation();
  const { resolveInteraction } = useMcpInteraction();
  const interaction = step.interaction;
  const properties = useMemo(
    () => schemaProperties(interaction?.schema || {}),
    [interaction?.schema],
  );
  const required = new Set(
    Array.isArray(interaction?.schema.required)
      ? interaction.schema.required.filter(
          (value): value is string => typeof value === "string",
        )
      : [],
  );
  const [values, setValues] = useState<Record<string, FieldValue>>(() =>
    initialValues(properties),
  );
  const [submitting, setSubmitting] = useState<
    "accept" | "decline" | "cancel" | null
  >(null);
  const [secondsLeft, setSecondsLeft] = useState<number | null>(
    typeof interaction?.timeout === "number"
      ? Math.round(interaction.timeout)
      : null,
  );

  useEffect(() => {
    if (secondsLeft === null || interaction?.decision) return;
    const timer = setInterval(() => {
      setSecondsLeft(previous =>
        previous === null || previous <= 0 ? 0 : previous - 1,
      );
    }, 1000);
    return () => clearInterval(timer);
  }, [secondsLeft === null, interaction?.decision]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!interaction) return null;

  const missingRequired = [...required].some(name => {
    const value = values[name];
    return value === undefined || value === "";
  });

  const resolve = async (action: McpInteractionAction) => {
    if (submitting) return;
    setSubmitting(action);
    try {
      const response = await resolveInteraction(
        interaction.execId,
        action,
        action === "accept" ? values : undefined,
      );
      if (!response.ok) {
        toast.error(await getErrorMessage(response));
        setSubmitting(null);
      }
    } catch (error) {
      logSSOTError(
        error,
        `McpInteractionCard:${action}:${interaction.execId}`,
      );
      toast.error(errorMsg("NETWORK_CONNECTION_ERROR"));
      setSubmitting(null);
    }
  };

  if (interaction.decision) {
    return (
      <div className="mt-1 text-xs opacity-70">
        {t(`chat.mcp_interaction.decision.${interaction.decision}`)}
      </div>
    );
  }

  return (
    <div className="mt-1.5 flex flex-col gap-3 rounded-xl border border-primary/30 bg-primary/5 p-3">
      <div className="text-xs font-bold uppercase tracking-wider">
        {t("chat.mcp_interaction.title")}
      </div>
      <p className="m-0 whitespace-pre-wrap text-sm">{interaction.message}</p>
      <div className="flex flex-col gap-2">
        {Object.entries(properties).map(([name, property]) => {
          const label = property.title || name;
          const id = `${interaction.execId}-${name}`;
          const value = values[name];
          return (
            <label key={name} htmlFor={id} className="flex flex-col gap-1 text-xs">
              <span className="font-bold">
                {label}
                {required.has(name) ? " *" : ""}
              </span>
              {property.type === "boolean" ? (
                <input
                  id={id}
                  aria-label={label}
                  type="checkbox"
                  checked={value === true}
                  onChange={event =>
                    setValues(previous => ({
                      ...previous,
                      [name]: event.target.checked,
                    }))
                  }
                />
              ) : property.enum ? (
                <select
                  id={id}
                  aria-label={label}
                  value={
                    value === undefined
                      ? ""
                      : String(
                          property.enum.findIndex(option =>
                            Object.is(option, value),
                          ),
                        )
                  }
                  onChange={event => {
                    const index = Number.parseInt(event.target.value, 10);
                    if (Number.isNaN(index)) return;
                    const selected = property.enum?.[index];
                    if (
                      typeof selected !== "string" &&
                      typeof selected !== "number" &&
                      typeof selected !== "boolean"
                    ) {
                      return;
                    }
                    setValues(previous => ({
                      ...previous,
                      [name]: selected,
                    }));
                  }}
                  className="rounded border border-border bg-background px-2 py-1.5"
                >
                  <option value="">
                    {t("chat.mcp_interaction.select_placeholder")}
                  </option>
                  {property.enum.map((option, index) => (
                    <option key={`${index}:${String(option)}`} value={String(index)}>
                      {String(option)}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  id={id}
                  aria-label={label}
                  type={
                    property.type === "integer" || property.type === "number"
                      ? "number"
                      : "text"
                  }
                  step={property.type === "integer" ? 1 : "any"}
                  value={
                    typeof value === "string" || typeof value === "number"
                      ? value
                      : ""
                  }
                  onChange={event => {
                    const next =
                      property.type === "integer"
                        ? Number.parseInt(event.target.value, 10)
                        : property.type === "number"
                          ? Number.parseFloat(event.target.value)
                          : event.target.value;
                    setValues(previous => ({
                      ...previous,
                      [name]: Number.isNaN(next) ? "" : next,
                    }));
                  }}
                  className="rounded border border-border bg-background px-2 py-1.5"
                />
              )}
              {property.description && (
                <span className="opacity-60">{property.description}</span>
              )}
            </label>
          );
        })}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          className="h-7 gap-1 px-3 text-xs font-bold"
          disabled={!!submitting || missingRequired}
          onClick={() => resolve("accept")}
        >
          <Send size={12} />
          {t("chat.mcp_interaction.accept")}
        </Button>
        <Button
          size="sm"
          variant="outline"
          className="h-7 gap-1 px-3 text-xs"
          disabled={!!submitting}
          onClick={() => resolve("decline")}
        >
          <X size={12} />
          {t("chat.mcp_interaction.decline")}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          className="h-7 gap-1 px-3 text-xs"
          disabled={!!submitting}
          onClick={() => resolve("cancel")}
        >
          <CircleStop size={12} />
          {t("chat.mcp_interaction.cancel")}
        </Button>
        {secondsLeft !== null && (
          <span className="ml-auto text-xs tabular-nums opacity-50">
            {t("chat.mcp_interaction.countdown", { seconds: secondsLeft })}
          </span>
        )}
      </div>
    </div>
  );
}
