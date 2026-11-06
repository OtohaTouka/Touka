/**
 * 聊天上傳分類（SSOT，純函式）— 依型別/大小把上傳檔路由為：圖片（vision）、文字內嵌、
 * 或不支援。Phase 1 只處理圖片與文字類；pdf/docx/xlsx 等二進位文件屬 Phase 2（需後端存檔）。
 *
 * 純函式、零相依、可單元測試——分類邏輯集中一處，UI（ChatArea/onPaste）只呼叫。
 */

/** 文字檔內嵌上限（超過則改走 Phase 2 上傳存檔，避免塞爆 context）。 */
export const MAX_TEXT_INLINE_BYTES = 64 * 1024;
/** 圖片上限（base64 膨脹 ~33% 後仍須 < 10MB 後端中介層）。 */
export const MAX_IMAGE_BYTES = 7 * 1024 * 1024;
/** 上傳文件上限（對齊後端 UPLOAD_MAX_BYTES，< 10MB 中介層）。 */
export const MAX_UPLOAD_BYTES = 8 * 1024 * 1024;
/** 貼上文字超過此字元數 → 收成附件（而非灌進輸入框）。 */
export const MAX_PASTE_INLINE_CHARS = 8000;

// Phase 2：二進位文件（存進 agent vault、由 AI 用工具讀），與後端 upload_service 白名單一致。
const DOC_EXTENSIONS: ReadonlySet<string> = new Set(["pdf", "docx", "xlsx", "pptx"]);

// 文字類副檔名白名單（補 MIME 偵測不準時，如 .md/.csv/程式碼常無正確 MIME）。
const TEXT_EXTENSIONS: ReadonlySet<string> = new Set([
  "txt", "md", "markdown", "csv", "tsv", "log", "json", "xml", "yaml", "yml", "toml", "ini", "env",
  "html", "htm", "css", "scss", "less", "js", "jsx", "ts", "tsx", "mjs", "cjs", "py", "rb", "go",
  "rs", "java", "kt", "c", "h", "cpp", "hpp", "cc", "cs", "php", "sh", "bash", "zsh", "sql", "r",
  "lua", "swift", "dart", "vue", "svelte", "tex", "rst", "diff", "patch",
]);

export type UploadKind = "image" | "text" | "document" | "unsupported";
export type UploadReason = "too_large" | "unsupported_type";

export interface UploadClassification {
  kind: UploadKind;
  reason?: UploadReason;
}

export interface UploadFileMeta {
  name: string;
  type: string;
  size: number;
}

/** 取小寫副檔名（無副檔名回空字串）。 */
export function fileExtension(name: string): string {
  const i = name.lastIndexOf(".");
  return i > 0 ? name.slice(i + 1).toLowerCase() : "";
}

/** 是否為文字類檔（MIME 或副檔名命中）。 */
export function isTextLike(file: { name: string; type: string }): boolean {
  if (file.type.startsWith("text/")) return true;
  if (file.type === "application/json" || file.type === "application/xml") return true;
  return TEXT_EXTENSIONS.has(fileExtension(file.name));
}

/**
 * 分類一個上傳檔。圖片→'image'（過大為 unsupported/too_large）；文字類→'text'（過大為
 * unsupported/too_large）；其餘→'unsupported'/unsupported_type（Phase 1 範圍外）。
 */
export function classifyUpload(file: UploadFileMeta): UploadClassification {
  if (file.type.startsWith("image/")) {
    return file.size > MAX_IMAGE_BYTES ? { kind: "unsupported", reason: "too_large" } : { kind: "image" };
  }
  // 二進位文件（pdf/docx/xlsx/pptx）→ 上傳存檔（Phase 2，AI 用工具讀）。
  if (DOC_EXTENSIONS.has(fileExtension(file.name))) {
    return file.size > MAX_UPLOAD_BYTES ? { kind: "unsupported", reason: "too_large" } : { kind: "document" };
  }
  if (isTextLike(file)) {
    // 小文字檔→內嵌；超過內嵌上限但在上傳上限內→當文件上傳存檔；再超過→過大。
    if (file.size <= MAX_TEXT_INLINE_BYTES) return { kind: "text" };
    return file.size > MAX_UPLOAD_BYTES ? { kind: "unsupported", reason: "too_large" } : { kind: "document" };
  }
  return { kind: "unsupported", reason: "unsupported_type" };
}

/**
 * 把文字檔內容包成可內嵌使用者訊息的區塊（清楚分隔、刻意不用 ``` fence 以免與檔案內容裡的
 * fence 衝突）。送往模型的 prompt 內容，故用通用英文標記、非 i18n。
 */
/**
 * 去除檔名中可能擾亂提示結構的字元（換行/方括號），防惡意檔名注入提示指令（縱深防禦，複審 Low）。
 */
function sanitizeDisplayName(name: string): string {
  return name.replace(/[\r\n[\]]/g, "_").slice(0, 128);
}

export function buildTextInlinePart(name: string, text: string): string {
  const safe = sanitizeDisplayName(name);
  return `\n\n[Attached file: ${safe}]\n${text}\n[End of file: ${safe}]\n`;
}

/**
 * 已上傳文件（存於 agent vault）的提示，注入使用者訊息文字：只說檔案存在哪裡。
 * relpath 為相對該 agent personal vault 的路徑（後端 upload 端點回傳）。送往模型，故用通用英文。
 * docgen 這類文件工具目前不能用（ADR-0293），所以不叫模型用它讀 PDF／Office 檔。
 */
export function buildFileRefHint(name: string, relpath: string): string {
  const safe = sanitizeDisplayName(name); // 伺服器已淨化，仍縱深防禦
  return (
    `\n\n[Uploaded file: ${safe} (saved at: ${relpath} in your own vault). ` +
    `Plain-text files can be read with the read tool (scope "personal").]\n`
  );
}

/** 人類可讀的位元組大小（chip 顯示用）。 */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
