/**
 * ADR-0318：上一輪沒做完的記號。被使用者停止（generationStopped）或出錯（generationFailed）的
 * 那則 AI 回覆，組下一輪的 API 訊息時在 content 最前面加一行；角色仍是 assistant、回合數不變。
 * ADR-0322：SNS 回覆送不出去（deliveryFailed）的再加一行「沒有送到」，排在停止／出錯之後。
 * 給模型看的固定文字（不是畫面文字，不走 i18n）；後端 utils/interjection_notes.py 同名常數
 * 要一起改（跨端一致性測試以純文字比對）。
 */
export const TURN_STOPPED_NOTE = '〔本回合被使用者停止，沒有做完〕';
export const TURN_FAILED_NOTE = '〔本回合出錯，沒有完成〕';
export const TURN_UNDELIVERED_NOTE_TEMPLATE = '〔這則回覆沒有送到 {platform}〕';
export const DELIVERY_PLATFORM_NAMES: Record<string, string> = { discord: 'Discord', telegram: 'Telegram' };
export const OTHER_PLATFORM_NAME = '外部平台';

type TurnFlags = {
  generationStopped?: boolean;
  generationFailed?: boolean;
  deliveryFailed?: boolean;
  deliveryPlatform?: string;
};

/** 認得的平台回固定名稱，其他回 undefined（畫面另外用 i18n 寫「外部平台」）。 */
export function knownPlatformName(platform: unknown): string | undefined {
  return typeof platform === 'string' ? DELIVERY_PLATFORM_NAMES[platform.toLowerCase()] : undefined;
}

/** 給模型看的平台名稱：看不懂的寫外部平台。 */
function deliveryPlatformName(platform: unknown): string {
  return knownPlatformName(platform) ?? OTHER_PLATFORM_NAME;
}

/** 被停止優先於出錯；沒有送到排在後面；只認 true。content 是空的就只留記號。 */
export function withTurnEndNote(content: string, node: TurnFlags): string {
  const end = node.generationStopped === true ? TURN_STOPPED_NOTE
    : node.generationFailed === true ? TURN_FAILED_NOTE : null;
  const undelivered = node.deliveryFailed === true
    ? TURN_UNDELIVERED_NOTE_TEMPLATE.replace('{platform}', deliveryPlatformName(node.deliveryPlatform))
    : null;
  const notes = [end, undelivered].filter((note): note is string => note !== null);
  if (notes.length === 0) return content;
  const note = notes.join('\n');
  return content ? `${note}\n\n${content}` : note;
}
