import { ERRORS, type ErrorCode } from "./error_codes";
import i18n from "@/lib/i18n";

export interface ToukaError {
  id: number;
  status: number;
  msg: string;
  i18n?: string;
}

/**
 * 帶後端錯誤碼的 API 錯誤（ToukaException 的 detail.code）。
 * 是 Error 子類別，既有 catch／message 用法不受影響。`reason`＝detail.reason（如 hook 擋下的理由）。
 */
export class ToukaApiError extends Error {
  readonly status: number;
  readonly code?: string;
  readonly reason?: string;

  constructor(message: string, status: number, code?: string, reason?: string) {
    super(message);
    this.name = "ToukaApiError";
    this.status = status;
    this.code = code;
    this.reason = reason;
  }
}

/** 取出 SSOT 認得的後端錯誤碼；不是 ToukaApiError 或碼不在 ERRORS 時回 undefined。 */
export function getErrorCode(error: unknown): ErrorCode | undefined {
  const code = error instanceof ToukaApiError ? error.code : undefined;
  return code !== undefined && Object.prototype.hasOwnProperty.call(ERRORS, code) ? (code as ErrorCode) : undefined;
}

/** 從失敗回應讀出 SSOT 認得的後端錯誤碼（讀 clone，不消耗原 body）；讀不到或不認得回 undefined。 */
export async function getResponseErrorCode(res: Response): Promise<ErrorCode | undefined> {
  try {
    const body: unknown = await res.clone().json();
    const detail = body && typeof body === "object" ? (body as { detail?: unknown }).detail : undefined;
    const code = detail && typeof detail === "object" ? (detail as { code?: unknown }).code : undefined;
    return typeof code === "string" && Object.prototype.hasOwnProperty.call(ERRORS, code) ? (code as ErrorCode) : undefined;
  } catch {
    return undefined;
  }
}

/** 從失敗回應的 detail 讀一個字串欄位（讀 clone，不消耗原 body）；沒有或不是字串回 undefined。 */
export async function getResponseDetailString(res: Response, field: string): Promise<string | undefined> {
  try {
    const body: unknown = await res.clone().json();
    const detail = body && typeof body === "object" ? (body as { detail?: unknown }).detail : undefined;
    const value = detail && typeof detail === "object" ? (detail as Record<string, unknown>)[field] : undefined;
    return typeof value === "string" ? value : undefined;
  } catch {
    return undefined;
  }
}

/** 建立靈魂或自訂模型時撞名：說出撞到哪個 ID；不是撞名回 undefined（交給一般訊息）。 */
export async function getCollisionMessage(res: Response): Promise<string | undefined> {
  if ((await getResponseErrorCode(res)) !== "AGENT_ID_COLLECTION_COLLISION") return undefined;
  const id = await getResponseDetailString(res, "collides_with");
  return id ? i18n.t("error.agent_id.collection_collision_with", { id }) : undefined;
}

/**
 * 取得錯誤碼的在地化顯示訊息。
 * 用全域 i18n 實例（非 React hook），可在元件 / hook / 純函式任意處呼叫；
 * catalog 缺該 i18n key 時以 SSOT 的 msg 為 defaultValue 回退（後端新增錯誤但前端尚未補翻時不致空白）。
 */
export function errorMsg(code: ErrorCode): string {
  const e = ERRORS[code];
  return i18n.t(e.i18n, { defaultValue: e.msg });
}

/**
 * 獲取 API 錯誤的顯示訊息
 * 
 * @param error 原始錯誤物件 (可能是 Response, AxiosError 或通用 Error)
 * @param fallbackCode 當無法識別錯誤時的保底 SSOT 常數
 * @returns 錯誤訊息字串
 */
export async function getErrorMessage(error: unknown, fallbackCode: ErrorCode = "SYSTEM_ERROR"): Promise<string> {
  // 1. 提取錯誤數據
  let responseData: unknown = (error as { response?: { data?: unknown } })?.response?.data;

  // 處理原生 Fetch Response 物件
  if (error instanceof Response) {
    try {
      // 注意：這裡假設 body 尚未被讀取。如果已讀取會噴錯
      responseData = await error.clone().json();
    } catch {
      // 非 JSON 格式，忽略
    }
  } 
  // 如果傳入的本身就是錯誤數據物件
  else if (error && typeof error === "object" && !responseData) {
    const errObj = error as Record<string, unknown>;
    if (errObj.id !== undefined || errObj.detail !== undefined) {
      responseData = error;
    }
  }

  if (responseData && typeof responseData === "object") {
    const dataObj = responseData as Record<string, unknown>;
    const errorDetail = dataObj.detail;
    // ToukaException 的實際 wire 形狀：FastAPI HTTPException 把 detail 再包一層 →
    // {"detail": {"code","id","message","i18n"}}；id/i18n 在巢內、頂層沒有。
    // 只認頂層會讓所有後端結構化錯誤落到 JSON.stringify 原始輸出。
    const nestedDetail =
      errorDetail && typeof errorDetail === "object" && !Array.isArray(errorDetail)
        ? (errorDetail as Record<string, unknown>)
        : undefined;
    // 優先尋找與 ERRORS 常數匹配的 id（頂層 legacy 形狀 → 巢內 ToukaException 形狀）
    const errorId = dataObj.id ?? nestedDetail?.id;

    // 嘗試從 ERRORS 中尋找匹配的定義
    const matchedError = Object.values(ERRORS).find((e: unknown) => (e as { id?: unknown }).id === errorId);
    if (matchedError) {
      const e = matchedError as { i18n?: string; msg: string };
      return e.i18n ? i18n.t(e.i18n, { defaultValue: e.msg }) : e.msg;
    }

    // 如果有 detail 字串且不是 Object，直接回傳 (通常是 FastAPI 的預設報錯)
    if (typeof errorDetail === "string") {
      return errorDetail;
    }

    // 巢內帶人類可讀 message（前端 SSOT 尚未認識的新錯誤碼）：優雅退路，不丟原始 JSON
    if (typeof nestedDetail?.message === "string") {
      return nestedDetail.message;
    }

    // 如果 detail 是物件或陣列 (例如 FastAPI 驗證錯誤)，將其序列化為可讀字串
    if (errorDetail && typeof errorDetail === "object") {
      return JSON.stringify(errorDetail);
    }
  }

  // 2. 處理網路超時或無法連線
  const errObj = error as Record<string, unknown>;
  if (errObj?.code === "ECONNABORTED" || (errObj?.message as string | undefined)?.includes("Network Error")) {
    return i18n.t("error.network.timeout");
  }

  // 3. 保底回傳 SSOT 定義的在地化訊息
  return errorMsg(fallbackCode);
}

/**
 * 將錯誤記錄到控制台 (遵循 SSOT 規範，不使用硬編碼字串)
 */
export function logSSOTError(error: unknown, context: string) {
  const msg = error instanceof Error ? error.message : String(error);
  console.error(`[${context}] Error:`, msg, error); // noqa: CONSOLE_BAN
}
