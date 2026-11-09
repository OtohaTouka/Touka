/** 把後端的錯誤回應換成看得懂的說明；後端沒給說明時才顯示狀態碼。 */
export async function proxyErrorMessage(response: Response): Promise<string> {
    const fallback = `Proxy Error: ${response.status}`;
    try {
        const body: unknown = await response.json();
        const detail = body && typeof body === 'object' ? (body as { detail?: unknown }).detail : undefined;
        if (typeof detail === 'string' && detail) { return detail; }
        const message = detail && typeof detail === 'object' ? (detail as { message?: unknown }).message : undefined;
        return typeof message === 'string' && message ? message : fallback;
    } catch {
        return fallback;
    }
}
