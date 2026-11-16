/**
 * ADR-0318：上一輪沒做完的記號。被使用者停止（generationStopped）或出錯（generationFailed）的
 * 那則 AI 回覆，組下一輪的 API 訊息時在 content 最前面加一行；角色仍是 assistant、回合數不變。
 * 給模型看的固定文字（不是畫面文字，不走 i18n）；後端 utils/interjection_notes.py 同名常數
 * 要一起改（跨端一致性測試以純文字比對）。
 */
export const TURN_STOPPED_NOTE = '〔本回合被使用者停止，沒有做完〕';
export const TURN_FAILED_NOTE = '〔本回合出錯，沒有完成〕';

/** 被停止優先於出錯；只認 true。content 是空的就只留記號。 */
export function withTurnEndNote(
  content: string,
  node: { generationStopped?: boolean; generationFailed?: boolean },
): string {
  const note = node.generationStopped === true ? TURN_STOPPED_NOTE
    : node.generationFailed === true ? TURN_FAILED_NOTE : null;
  if (!note) return content;
  return content ? `${note}\n\n${content}` : note;
}
