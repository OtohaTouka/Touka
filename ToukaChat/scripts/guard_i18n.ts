/**
 * Guard #33 — i18n 翻譯目錄 key-sync 守衛（社群翻譯防線）。
 *
 * 以來源語言（zh-TW）為基準，驗證每個語言檔：
 *   - JSON 合法可解析。
 *   - 無「多餘 key」（不在來源、且不是來源 key 的 i18next 複數變體）→ 錯誤（過時/拼錯）。
 *   - en（國際參考語言）必須 100% 覆蓋來源 → 缺 key 為錯誤。
 *   - 其餘社群語言缺 key → 警告（fallback 補上，允許漸進翻譯）。
 *   - 空字串 value → 警告（會渲染空白）。
 *   - 檔案系統的語言目錄與 src/locales/index.ts 註冊表一致。
 *
 * 動態鍵（如 theme.names.<key>）為一般 catalog key，已在來源 → 自然納入比對。
 * 複數鍵（key_one/_other…）為語言特有，允許在語言檔多出（其 base 在來源即可）。
 */
import fs from "node:fs";
import path from "node:path";
import { SUPPORTED_LNGS, SOURCE_LNG } from "../src/locales";

const __filename = new URL(import.meta.url).pathname.substring(
  process.platform === "win32" ? 1 : 0,
);
const PROJECT_ROOT = path.resolve(path.dirname(__filename), "..");
const LOCALES_DIR = path.join(PROJECT_ROOT, "src", "locales");
const PLURAL_SUFFIX = /_(zero|one|two|few|many|other)$/;

type Flat = Map<string, string>;

function flatten(obj: unknown, prefix = "", out: Flat = new Map()): Flat {
  for (const [k, v] of Object.entries(obj as Record<string, unknown>)) {
    const key = prefix ? `${prefix}.${k}` : k;
    if (v !== null && typeof v === "object" && !Array.isArray(v)) {
      flatten(v, key, out);
    } else {
      out.set(key, String(v));
    }
  }
  return out;
}

function readLocale(lng: string): { flat?: Flat; error?: string } {
  const fp = path.join(LOCALES_DIR, lng, "translation.json");
  if (!fs.existsSync(fp)) return { error: `missing ${path.relative(PROJECT_ROOT, fp)}` };
  try {
    return { flat: flatten(JSON.parse(fs.readFileSync(fp, "utf-8"))) };
  } catch (e) {
    return { error: `invalid JSON in ${lng}/translation.json: ${(e as Error).message}` };
  }
}

const baseOf = (k: string): string => k.replace(PLURAL_SUFFIX, "");

function main(): void {
  console.log("🔍 Touka i18n Key-Sync Guard (#33) 正在啟動...");
  const errors: string[] = [];
  const warnings: string[] = [];

  // 0) 檔案系統目錄 ↔ 註冊表一致
  const dirs = fs
    .readdirSync(LOCALES_DIR, { withFileTypes: true })
    .filter((d) => d.isDirectory())
    .map((d) => d.name);
  for (const d of dirs) {
    if (!SUPPORTED_LNGS.includes(d)) {
      errors.push(`目錄 src/locales/${d}/ 存在，但未在 src/locales/index.ts 註冊（LOCALES）`);
    }
  }
  for (const l of SUPPORTED_LNGS) {
    if (!dirs.includes(l)) errors.push(`'${l}' 已註冊，但找不到 src/locales/${l}/ 目錄`);
  }

  // 1) 來源語言
  const src = readLocale(SOURCE_LNG);
  if (src.error) {
    console.error(`🚨 來源語言 ${SOURCE_LNG} 無法讀取：${src.error}`);
    process.exit(1);
  }
  const sourceKeys = new Set(src.flat!.keys());

  // 2) 各語言對來源比對
  for (const lng of SUPPORTED_LNGS) {
    if (lng === SOURCE_LNG) continue;
    const { flat, error } = readLocale(lng);
    if (error) {
      errors.push(`${lng}: ${error}`);
      continue;
    }
    const keys = flat!;
    const empty = [...keys.entries()].filter(([, v]) => v.trim() === "").map(([k]) => k);
    const extra = [...keys.keys()].filter(
      (k) => !sourceKeys.has(k) && !(PLURAL_SUFFIX.test(k) && sourceKeys.has(baseOf(k))),
    );
    const covered = new Set<string>();
    for (const k of keys.keys()) {
      covered.add(k);
      covered.add(baseOf(k));
    }
    const missing = [...sourceKeys].filter((k) => !covered.has(k));

    if (extra.length) {
      errors.push(
        `${lng}: ${extra.length} 個多餘 key（不在來源 ${SOURCE_LNG}）— 例：${extra.slice(0, 5).join(", ")}`,
      );
    }
    if (empty.length) {
      warnings.push(`${lng}: ${empty.length} 個空值 — 例：${empty.slice(0, 5).join(", ")}`);
    }
    if (missing.length) {
      const msg = `${lng}: 缺 ${missing.length} 個 key（vs 來源）— 例：${missing.slice(0, 5).join(", ")}`;
      if (lng === "en") errors.push(`${msg}（'en' 為參考語言，必須 100% 完整）`);
      else warnings.push(msg);
    }
  }

  if (warnings.length) {
    console.warn("⚠️  i18n 警告（不阻擋）：");
    warnings.forEach((w) => console.warn("  - " + w));
  }
  if (errors.length) {
    console.error("🚨 [I18N_KEYSYNC_VIOLATION] 翻譯目錄 key 不同步：");
    errors.forEach((e) => console.error("  - " + e));
    console.error(`\n❌ 共 ${errors.length} 項違規。`);
    process.exit(1);
  }
  console.log(
    `🎉 i18n key-sync 通過：${SUPPORTED_LNGS.length} 種語言、來源 ${sourceKeys.size} 個 key，全數同步。`,
  );
  process.exit(0);
}

main();
