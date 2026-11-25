import type { TFunction } from "i18next";
import type { ErrorCode } from "./error_codes";
import { errorMsg } from "./error_handler";
import { SPACE_REASONS } from "./project_save_wording";

/** 下載專案被拒絕時伺服器給的錯誤碼與原因（`detail.code`、`detail.reason`）。 */
export interface ProjectDownloadFailure {
  code?: ErrorCode;
  reason?: string;
}

/** 下載被拒的原因（ADR-0332 PR B）：422 是名單或形狀的問題，413 是超過上限。 */
const VALIDATION_REASONS: ReadonlySet<string> = new Set([
  "not_dedicated_space",
  "no_git",
  "git_not_folder",
  "git_is_link",
  "no_index",
  "index_unsupported",
  "index_invalid",
  "protected_location",
  "outside_workspace",
  "scan_limit",
]);
const TOO_LARGE_REASONS: ReadonlySet<string> = new Set([
  "index_too_large",
  "too_many_files",
  "too_large",
  "walk_too_large",
]);

/** 下載失敗時給人看的一句話：下載的原因 → 專用空間的形狀 → 下載用的錯誤碼 → 一般的「下載失敗」。 */
export function projectDownloadFailureText(t: TFunction, failure: ProjectDownloadFailure): string {
  const { code, reason } = failure;
  if (code === "VALIDATION_ERROR" && reason) {
    if (VALIDATION_REASONS.has(reason)) return t(`projects.projects_view.download_reasons.${reason}`);
    if (SPACE_REASONS.has(reason)) return t(`projects.projects_view.space_reasons.${reason}`);
  }
  if (code === "REQUEST_TOO_LARGE" && reason && TOO_LARGE_REASONS.has(reason)) {
    return t(`projects.projects_view.download_reasons.${reason}`);
  }
  if (code === "PROJECT_DOWNLOAD_BUSY" || code === "PROJECT_FOLDER_TAKEN") return errorMsg(code);
  if (code === "PROJECT_FOLDER_MISSING") return t("projects.projects_view.download_folder_missing");
  if (code === "AUTH_PERMISSION_DENIED") return t("projects.projects_view.download_not_allowed");
  return t("projects.projects_view.download_failed");
}

const MAX_NAME_CHARS = 60;
const FALLBACK_NAME = "project";
/** 檔名不能用的字元（Windows 的保留字元）與會偽裝檔名的雙向控制字元。 */
const UNSAFE_CHARS: ReadonlySet<string> = new Set([
  ..."<>:\"/\\|?*",
  "؜", "‎", "‏", "‪", "‫", "‬", "‭", "‮",
  "⁦", "⁧", "⁨", "⁩",
]);

function isUnsafe(ch: string): boolean {
  const code = ch.codePointAt(0) ?? 0;
  return code < 0x20 || code === 0x7f || UNSAFE_CHARS.has(ch);
}

/** 下載的檔名：專案名稱（不安全的字元換成 `_`、去掉頭尾的空白與點、截短）加日期。 */
export function projectDownloadFileName(name: string, now: Date): string {
  const replaced = Array.from(name, (ch) => (isUnsafe(ch) ? "_" : ch)).join("");
  const trimmed = replaced.replace(/^[\s.]+|[\s.]+$/g, "");
  const stem = Array.from(trimmed).slice(0, MAX_NAME_CHARS).join("") || FALLBACK_NAME;
  return `${stem}-${now.toISOString().slice(0, 10)}.zip`;
}
