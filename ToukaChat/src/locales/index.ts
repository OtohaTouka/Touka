// 語言註冊表（i18n SSOT）。
//
// 新增一種語言 = 三步：
//   1. 放檔：src/locales/<lng>/translation.json（複製 en/ 後翻譯 value）。
//   2. 在下方 import 該檔，並加入 LOCALES 一行。
//   3. 在 LOCALE_ENDONYMS 加一行（語言以自身文字書寫的名稱，給語言切換鈕顯示）。
// supportedLngs / resources 會自動衍生，i18n init、守衛 #33、語言切換 UI 都讀這份註冊表。
//
// 來源語言 = zh-TW（作者撰寫、永遠完整）；en 為國際參考語言；其餘為社群翻譯。
import zhTW from "./zh-TW/translation.json";
import en from "./en/translation.json";

/** 來源語言：所有其他語言以此為 key 對齊基準（守衛 #33）。 */
export const SOURCE_LNG = "zh-TW";

/** 語言代碼 → 翻譯目錄（內部註冊表；新增語言在此加一行）。SUPPORTED_LNGS / i18nResources 由此衍生。 */
const LOCALES: Record<string, Record<string, unknown>> = {
  "zh-TW": zhTW,
  en,
};

/**
 * 語言代碼 → 該語言以「自身文字」書寫的名稱（endonym）。語言切換 UI 直接顯示這份，
 * 刻意不進 i18n catalog（語言名以自身語言呈現，不隨介面語言翻譯）。新增語言時補一行。
 */
export const LOCALE_ENDONYMS: Record<string, string> = {
  "zh-TW": "繁體中文",
  en: "English",
};

/** i18next supportedLngs（由 LOCALES 自動衍生）。 */
export const SUPPORTED_LNGS = Object.keys(LOCALES);

/** i18next resources（由 LOCALES 自動衍生，單一 'translation' namespace）。 */
export const i18nResources: Record<string, { translation: Record<string, unknown> }> =
  Object.fromEntries(
    Object.entries(LOCALES).map(([lng, translation]) => [lng, { translation }]),
  );
