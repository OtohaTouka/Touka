"use client";

import type { ReactNode } from "react";
import { I18nextProvider } from "react-i18next";
import i18n from "@/lib/i18n";

/**
 * 將已配置的 i18next 實例注入 React 樹（client-only）。
 * 取代已退役的自製 i18nContext；底層改用 react-i18next。
 */
export function I18nProvider({ children }: { children: ReactNode }) {
  return <I18nextProvider i18n={i18n}>{children}</I18nextProvider>;
}
