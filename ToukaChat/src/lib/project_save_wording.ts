import type { TFunction } from "i18next";
import type { ErrorCode } from "./error_codes";
import { errorMsg } from "./error_handler";

/** 存專案被拒絕時伺服器給的錯誤碼與原因（`detail.code`、`detail.reason`）。 */
export interface ProjectSaveFailure {
  code?: ErrorCode;
  reason?: string;
}

/** 專用空間（ADR-0332）的登記被拒絕的原因：每個都有自己的說明（下載被拒時也用）。 */
export const SPACE_REASONS: ReadonlySet<string> = new Set([
  "project_space_root",
  "project_not_top_level",
  "project_name_unsafe",
  "project_linked",
  "project_space_unavailable",
  "project_path_form",
]);

/** 清 HOME 前被擋下、等再久也不會自己好的原因（掃描、執行器設定）：說原因，不說「稍後再試」。 */
const HOME_REASONS: ReadonlySet<string> = new Set([
  "protected_location",
  "scan_limit",
  "executor_no_project_root",
  "executor_project_root_mismatch",
]);

/** 專用空間登記專用的錯誤碼：用錯誤碼自己的說明。 */
const SPACE_CODES: ReadonlySet<ErrorCode> = new Set<ErrorCode>([
  "PROJECT_FOLDER_TAKEN",
  "PROJECT_FOLDER_MISSING",
  "PROJECT_HOME_NOT_CLEARED",
]);

/** 存專案失敗時給人看的一句話：專用空間的原因 → 清不掉 HOME 的原因 → 專用空間的錯誤碼 → 一般的「存不了」。 */
export function projectSaveFailureText(t: TFunction, failure: ProjectSaveFailure): string {
  const { code, reason } = failure;
  if (code === "VALIDATION_ERROR" && reason && SPACE_REASONS.has(reason)) {
    return t(`projects.projects_view.space_reasons.${reason}`);
  }
  if (code === "PROJECT_HOME_NOT_CLEARED" && reason && HOME_REASONS.has(reason)) {
    return t(`projects.projects_view.home_reasons.${reason}`);
  }
  if (code && SPACE_CODES.has(code)) return errorMsg(code);
  return t("projects.projects_view.save_failed");
}
