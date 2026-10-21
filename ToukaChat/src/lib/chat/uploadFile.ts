import { fetchTouka, getActiveProxyUrl } from "@/lib/connections";

/** 後端 POST /v1/chat/upload 的回傳（對齊 upload_service.save_upload）。 */
export interface UploadedFile {
  filename: string;
  relpath: string;
  mime: string;
  size: number;
}

/**
 * 上傳非圖片文件到後端（存進當下 agent 自己的 vault），回 {filename, relpath, mime, size}。
 * 走 fetchTouka（Fetch SSOT＋自動帶 Auth），以 FormData multipart 傳送（瀏覽器自帶 boundary）。
 */
export async function uploadChatFile(file: File, sessionId: string): Promise<UploadedFile> {
  const form = new FormData();
  form.append("file", file);
  form.append("session_id", sessionId || "");
  const res = await fetchTouka(getActiveProxyUrl(), "/v1/chat/upload", { method: "POST", body: form });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return (await res.json()) as UploadedFile;
}
