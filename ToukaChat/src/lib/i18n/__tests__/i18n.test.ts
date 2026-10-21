import { describe, it, expect, afterAll } from "vitest";
import i18n from "@/lib/i18n";
import { SUPPORTED_LNGS, LOCALE_ENDONYMS, SOURCE_LNG } from "@/locales";

// i18n 是跨檔共享單例；sequence.shuffle 會打亂本檔 it 次序，使「還原 zh-TW」未必最後執行 →
// 本檔可能留下 en 污染同 fork 其他測試（如 McpTab 斷言中文）。afterAll 無條件還原，杜絕外溢。
afterAll(async () => { await i18n.changeLanguage("zh-TW"); });

/**
 * Phase 0 煙霧測試：證明 react-i18next 管線可運作——
 * 資源載入、key 解析、語言切換、未知 key 安全回退。
 * 每個語言相關案例先 changeLanguage 再斷言，故與隨機測試次序無關。
 */
describe("i18n (react-i18next) setup", () => {
  it.each(["zh-TW", "en"])("defines every system status without fallback in %s", lng => {
    for (const state of ["checking", "auth_required", "forbidden", "unavailable", "standalone", "hybrid"]) {
      const key = `sidebar.system_status.${state}`;
      expect(i18n.getResource(lng, "translation", key), key).toEqual(expect.any(String));
      expect(i18n.getResource(lng, "translation", key), key).not.toBe("");
    }
  });
  it.each(["zh-TW", "en"])("defines every reliability message without fallback in %s", lng => {
    for (const key of ["chat.persistence.pending", "chat.persistence.unknown", "chat.persistence.failed",
      "chat.persistence.recheck", "chat.persistence.retry", "hooks.use_session_actions.incognito_split_export_only",
      "hooks.use_session_actions.split_exported_save_unconfirmed", "settings.general_tab.toast_import_partial"]) {
      expect(i18n.getResource(lng, "translation", key), key).toEqual(expect.any(String));
      expect(i18n.getResource(lng, "translation", key), key).not.toBe("");
    }
  });
  it("initializes with zh-TW and en translation bundles", () => {
    expect(i18n.isInitialized).toBe(true);
    expect(i18n.hasResourceBundle("zh-TW", "translation")).toBe(true);
    expect(i18n.hasResourceBundle("en", "translation")).toBe(true);
  });

  it("resolves source (zh-TW) strings", async () => {
    await i18n.changeLanguage("zh-TW");
    expect(i18n.t("settings.language")).toBe("語言設定");
    expect(i18n.t("common.send")).toBe("發送");
  });

  it("switches to English", async () => {
    await i18n.changeLanguage("en");
    expect(i18n.t("settings.language")).toBe("Language Settings");
    expect(i18n.t("common.send")).toBe("Send");
  });

  it("covers Phase 1/2 English keys: interpolation + plurals render correctly", async () => {
    await i18n.changeLanguage("en");
    // 代表性 batch 1/2 keys 解析為英文
    expect(i18n.t("chat.message_item.copy")).toBe("Copy");
    expect(i18n.t("chat.context_meter.got_it")).toBe("Got It");
    expect(i18n.t("settings.tabs.sliding_window.title")).toBe("Sliding Window History");
    // 內插帶過變數
    expect(i18n.t("chat.chat_area.model_switched_style_may_change", { model: "GPT" }))
      .toBe("Switched to GPT — the response style may change");
    // 英文複數（i18next key_one / key_other）
    expect(i18n.t("sidebar.session_item.round_count", { count: 1 })).toBe("1 round");
    expect(i18n.t("sidebar.session_item.round_count", { count: 3 })).toBe("3 rounds");
    await i18n.changeLanguage("zh-TW"); // 還原，避免污染隨機次序測試
  });

  it("returns the key for unknown paths without throwing", () => {
    expect(i18n.t("nonexistent.key.demo")).toBe("nonexistent.key.demo");
  });
});

describe("locales barrel（語言註冊表 SSOT，P4 語言切換動態化）", () => {
  it("每個 SUPPORTED_LNGS 都有 LOCALE_ENDONYMS（語言切換鈕才不會顯示原始代碼）", () => {
    for (const lng of SUPPORTED_LNGS) {
      expect(LOCALE_ENDONYMS[lng], lng).toBeTruthy();
    }
  });

  it("來源語言 zh-TW 已註冊且 endonym 為繁體中文", () => {
    expect(SUPPORTED_LNGS).toContain(SOURCE_LNG);
    expect(LOCALE_ENDONYMS["zh-TW"]).toBe("繁體中文");
  });
});
