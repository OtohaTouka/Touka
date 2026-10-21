/**
 * HAX 基線守衛（H1 批）— 源碼掃描型，長期執法。
 *
 * 對照 Microsoft「Guidelines for Human-AI Interaction」H1 四項低成本修復，
 * 確保未來重構不會默默移除這些人機互動保護：
 *   G1  首屏能力揭露：ChatArea 空狀態使用 ChatWelcome
 *   G2  免責提示：ChatArea 常駐「請自行查證」一行
 *   G16 後果揭示：AuthSwitcher 對 YOLO 二次確認；SessionItem 刪除標示不可復原
 *   G18 版本顯示：GeneralTab「關於」區塊讀取 app_meta 版本（SSOT）
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const SRC = join(__dirname, '../../src');
const read = (rel: string) => readFileSync(join(SRC, rel), 'utf-8');

describe('HAX 基線守衛（H1）', () => {
  it('G1：ChatArea 空狀態以 ChatWelcome 揭露能力與範例提示', () => {
    const chatArea = read('components/chat/ChatArea.tsx');
    expect(chatArea).toMatch(/<ChatWelcome\b/);
  });

  it('G2：ChatArea 常駐 AI 免責提示（請自行查證）', () => {
    const chatArea = read('components/chat/ChatArea.tsx');
    // i18n 後免責文案改由 t('chat.chat_area.ai_disclaimer') 渲染（文案＝「請自行查證」，在 catalog）。
    expect(chatArea).toMatch(/chat\.chat_area\.ai_disclaimer/);
  });

  it('G16：AuthSwitcher 對 YOLO 升級先行二次確認', () => {
    const auth = read('components/chat/AuthSwitcher.tsx');
    expect(auth).toMatch(/confirmToast/);
    expect(auth).toMatch(/id === 'YOLO'/);
  });

  it('G16：SessionItem 刪除確認標示不可復原', () => {
    const item = read('components/sidebar/left/SessionItem.tsx');
    expect(item).toMatch(/不可復原/);
  });

  it('G18：GeneralTab「關於」區塊以 app_meta 為版本 SSOT', () => {
    const tab = read('components/settings/tabs/GeneralTab.tsx');
    expect(tab).toMatch(/app_meta/);
    expect(tab).toMatch(/APP_VERSION/);
    // i18n 後「關於」標題改由 t('settings.general_tab.about_heading') 渲染，
    // 故守衛改錨定該 i18n key（文案已移入 src/locales），意圖不變。
    expect(tab).toMatch(/about_heading/);
  });
});

describe('HAX 基線守衛（H2 — G15/G6 細粒度回饋）', () => {
  it('G15：MessageItem 串接讚/倒讚（ThumbsUp/ThumbsDown）', () => {
    const item = read('components/chat/MessageItem.tsx');
    expect(item).toMatch(/ThumbsUp/);
    expect(item).toMatch(/ThumbsDown/);
    expect(item).toMatch(/onFeedback/);
  });

  it('G15：ChatArea 持有 useFeedback 並渲染原因彈窗', () => {
    const chatArea = read('components/chat/ChatArea.tsx');
    expect(chatArea).toMatch(/useFeedback/);
    expect(chatArea).toMatch(/FeedbackReasonDialog/);
  });

  it('G15：系統監控頁掛載 FeedbackLog 檢視節點', () => {
    const view = read('components/sidebar/SystemMonitoringView.tsx');
    expect(view).toMatch(/FeedbackLog/);
  });
});

describe('HAX 基線守衛（G10 — ask_user 詢問使用者工具）', () => {
  it('G10：SSE 解析認得 ask_user_request 自訂事件', () => {
    const conn = read('lib/connections.ts');
    expect(conn).toMatch(/ask_user_request/);
  });

  it('G10：StreamRegistry onEvent 分發 ask_user_request 至回調', () => {
    const reg = read('lib/providers/StreamRegistryProvider.tsx');
    expect(reg).toMatch(/ask_user_request/);
  });

  it('G10：全域事件層渲染 AskUserModal 問卷彈窗', () => {
    const ctx = read('lib/SessionContext.tsx');
    expect(ctx).toMatch(/AskUserModal/);
  });
});

describe('HAX 基線守衛（G17 — 無痕模式）', () => {
  it('G17：syncSession 單一收口防線跳過無痕 session', () => {
    const provider = read('lib/providers/SessionListProvider.tsx');
    expect(provider).toMatch(/incognito/);
  });

  it('G17：connections body 帶 incognito 旗標（觸發後端閘門）', () => {
    const conn = read('lib/connections.ts');
    expect(conn).toMatch(/incognito/);
  });

  it('G17：LeftSidebar 提供無痕對話入口', () => {
    const sidebar = read('components/LeftSidebar.tsx');
    expect(sidebar).toMatch(/無痕|incognito/i);
  });

  it('G17 加固（⑤a）：無痕旗標具誕生登記兜底（不得只依賴 sessions 清單回查）', () => {
    // wipe 競態防護：refreshSessions 整批覆蓋會洗掉僅存記憶體的無痕 session，
    // 發送時旗標必須有 isIncognitoSession 兜底，否則無痕訊息會以一般模式送出。
    expect(read('lib/providers/ChatProvider.tsx')).toMatch(/isIncognitoSession/);
    expect(read('hooks/useChatStream.ts')).toMatch(/isIncognitoSession/);
  });
});

describe('HAX 基線守衛（H3 — G7/G14/G11/G12）', () => {
  it('G7：page.tsx 提供開新對話的全域鍵盤捷徑', () => {
    const page = read('app/page.tsx');
    expect(page).toMatch(/shiftKey/);
    expect(page).toMatch(/handleNewSession\(\)/);
  });

  it('G7：LeftSidebar 新增對話按鈕標示捷徑', () => {
    const sidebar = read('components/LeftSidebar.tsx');
    // i18n 後捷徑提示改由 t('layout.left_sidebar.new_chat_button_tooltip') 渲染（含 ⌘/Ctrl+Shift+O，在 catalog）。
    expect(sidebar).toMatch(/layout\.left_sidebar\.new_chat_button_tooltip/);
  });

  it('G14：ChatArea 切換模型時提示回應風格可能改變', () => {
    const chatArea = read('components/chat/ChatArea.tsx');
    expect(chatArea).toMatch(/回應風格/);
    expect(chatArea).toMatch(/toast\.info/);
  });

  it('G14：ModelsTab 標示開關/可見性變更即時生效', () => {
    const tab = read('components/settings/tabs/ModelsTab.tsx');
    expect(tab).toMatch(/即時生效/);
  });

  it('G11：ToolStepTracker 對記憶工具揭露使用了哪些記憶', () => {
    const tracker = read('components/chat/ToolStepTracker.tsx');
    expect(tracker).toMatch(/describeMemoryAccess/);
    expect(tracker).toMatch(/記憶/);
  });

  it('G12：ContextMeter 提供上下文載入說明面板', () => {
    const meter = read('components/chat/ContextMeter.tsx');
    expect(meter).toMatch(/Dialog/);
    // i18n 後說明面板文案改由 t() 渲染（標題＝「本次對話載入了什麼」、內文提到「滑動視窗」，皆在 catalog）。
    expect(meter).toMatch(/chat\.context_meter\.dialog_title_loaded/);
    expect(meter).toMatch(/chat\.context_meter\.loaded_detail/);
  });
});

describe('HAX 基線守衛（G9 — 生成中插話，ADR-0249）', () => {
  it('G9：ChatArea 生成中可排隊補充，排隊中的訊息可收回或修改', () => {
    const chatArea = read('components/chat/ChatArea.tsx');
    expect(chatArea).toMatch(/useInterjectionComposer/);
    expect(chatArea).toMatch(/<QueuedMessageList\b/);
    const list = read('components/chat/QueuedMessageList.tsx');
    expect(list).toMatch(/chat\.interjection\.retract/);
    expect(list).toMatch(/chat\.interjection\.edit/);
  });

  it('G9：Esc 停止生成，@ 選單開著時由 MentionInput 先關選單', () => {
    expect(read('lib/chat/interjections.ts')).toMatch(/'Escape'/);
    expect(read('components/ui/MentionInput.tsx')).toMatch(/e\.key === "Escape"[\s\S]*setShowMentions\(false\)/);
  });

  it('G2：工具時間軸標出補充在第幾步送達', () => {
    expect(read('components/chat/MessageItem.tsx')).toMatch(/<InterjectionMarker\b/);
    expect(read('components/chat/InterjectionMarker.tsx')).toMatch(/chat\.interjection\.delivered_at_step/);
  });
});
