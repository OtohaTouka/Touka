import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import LanguageDetector from "i18next-browser-languagedetector";

import { i18nResources, SUPPORTED_LNGS } from "@/locales";

/**
 * Touka 前端 i18n SSOT（react-i18next、client-only）。
 *
 * - 純前端切換：不做 SSR、不在 URL 帶語言（Touka 為自架應用軟體，SEO 價值低）。
 * - 翻譯目錄 SSOT：src/locales/<lng>/translation.json，一語言一檔，
 *   社群只要複製 en/ 改 value 即可新增語言。
 * - 語言記憶與首次偵測交由 i18next-browser-languagedetector（寫入 localStorage
 *   鍵 'touka_lang'）；本專案自有程式碼完全不直接觸碰 localStorage，因此仍
 *   符合 localStorage SSOT 守衛（禁令 9）——持久化由函式庫內部負責。
 * - fallbackLng 'en'（Phase 2 英文版已補齊、parity 驗證 813/813、placeholder/markup
 *   零落差）：英文為國際參考語言，社群語言缺 key 時回退英文最通用；zh-TW 為完整
 *   來源，主使用者偵測即得 zh-TW、不觸發回退。
 */
if (!i18n.isInitialized) {
  i18n
    .use(LanguageDetector)
    .use(initReactI18next)
    .init({
      resources: i18nResources,
      fallbackLng: "en",
      // supportedLngs 由語言註冊表（src/locales/index.ts）衍生：navigator 給 'en-US'
      // 會經 resolve hierarchy 退到 'en'、'zh-TW' 維持 'zh-TW'，不支援的語言退到
      // fallbackLng。註：刻意不開 nonExplicitSupportedLngs——它會把 'zh-TW' 收斂成
      // 不存在 bundle 的 'zh'，導致 t() 回傳原始 key（已於 config 矩陣測試證實）。
      supportedLngs: SUPPORTED_LNGS,
      defaultNS: "translation",
      interpolation: { escapeValue: false },
      detection: {
        order: ["localStorage", "navigator"],
        caches: ["localStorage"],
        lookupLocalStorage: "touka_lang",
      },
      react: { useSuspense: false },
    });
}

export default i18n;
