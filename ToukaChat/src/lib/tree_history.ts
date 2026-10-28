import type { ChatAttachment } from '@/lib/chat/attachment';
import type { Interjection } from '@/lib/types';
import type { RoundInterruption } from '@/lib/chat/interjections';

// iso-c2：審批卡上的隔離狀態。level 為 none 時 fallback 是人核准後的執行方式；
// 事件看不懂時一律成 level none、fallback null，卡片據此不讓核准（fail-closed）。
export type IsolationLevelName = 'none' | 'landlock' | 'executor';
export interface ToolStepIsolation {
  level: IsolationLevelName;
  fallback: 'executor' | 'landlock' | 'direct' | null;
  features: string[];                               // 生效的權利（tcp、signal…）
  reason?: string;                                  // 後端原因代碼
  reasonDetail?: string;                            // 原因細節（如 not_writable）
  expected?: IsolationLevelName;                    // 部署預期的層級（C2-2）
  net?: string;                                     // 降級去處那一層的網路設定問題
  blockPortsIssue?: string;                         // 封鎖埠擋不全（讀不到設定或溢位）
  localIssue?: string;                              // 讓降級只能「未隔離」的本機原因
  complete?: boolean;                               // false＝未隔離卡沒有完整顯示要執行的內容（C2-3）
}

// hitl-unification H3：統一審批卡（tool_approval_request/resolved 事件）的步驟附載。
export interface ToolStepApproval {
  execId: string;                                   // approve/reject 端點的解算鍵
  risk?: string;                                    // read_only | safe | dangerous
  timeout?: number;                                 // 逾時秒數（倒數顯示用）
  // 解算結果（含逾時自動拒；isolation_* 是核准沒算數的兩種隔離結果，approval_flow 的 verdict）
  decision?: 'approved' | 'rejected' | 'timeout' | 'isolation_unacknowledged' | 'isolation_incomplete';
  reviewReason?: string;                            // Auto 審查員轉人的理由（ADR-0246）
  hookReason?: string;                              // 管理員設定的 hook 要人確認（可為空字串，ADR-0292）
  humanOnly?: boolean;                              // 只有人能核准（底線；審查員與 YOLO 不能放行）
  isolation?: ToolStepIsolation;                    // 事件沒帶就照舊顯示（iso-c2）
}

export interface ToolStepInteraction {
  execId: string;
  kind: 'elicitation_form';
  message: string;
  schema: Record<string, unknown>;
  timeout?: number;
  decision?: 'accepted' | 'declined' | 'cancelled' | 'timeout';
}

export interface ToolStep {
  id: string;
  tool: string;
  args: unknown;
  // awaiting_approval：H3 審批卡狀態——人類批准後由 tool_start 就地升級為 running
  status: 'awaiting_approval' | 'running' | 'success' | 'error';
  result?: unknown;
  approval?: ToolStepApproval;
  interaction?: ToolStepInteraction;
}

// context 用量分解（ADR-0092 Phase B）：各類別 token 與占用百分比。
export interface ContextBreakdown {
  usage_pct: number;                 // input_tokens / 模型 window（封頂 100）
  window: number;                    // 模型 context window（token）
  total_tokens: number;              // 本回合輸入 token（authoritative）
  breakdown: Record<string, number>; // 類別（persona/capabilities/tools/conversation…）→ token 數
}

// 結構化進度（touka_progress）：Agent 複數行動的可觀察狀態，依 kind 收斂。
export interface AgentProgress {
  kind: string;            // react_slots | repair | group | creation_stage | tool_retry | token_usage
  current?: number;
  total?: number;
  attempt?: number;
  max?: number;
  index?: number;
  agent?: string;
  stage?: string;
  status?: string;
  tool?: string;
  target_kind?: string;
  cost?: number;
  usage_pct?: number;              // ADR-0092 Phase B：context window 占用百分比
  context_breakdown?: ContextBreakdown; // 分類別 token 分解（Claude 路徑提供）
}

// 以 kind 為鍵的進度表（每種機制只保留最新狀態）。
export type ProgressMap = Record<string, AgentProgress>;

export interface MessageNode {
  id: string;
  parentId: string | null;
  childrenIds: string[];
  role: "user" | "assistant" | "system";
  content: string;
  model?: string;
  createdAt?: number;
  /** 訊息實際時間（毫秒）：使用者訊息在送出時、助理回覆在完成時由前端蓋；
   *  後端寫的節點（心跳等）也用這個欄位。已有的值寫回時一律保留；匯入舊檔可能是秒。 */
  timestamp?: number;
  attachment?: ChatAttachment;
  steps?: ToolStep[];
  progress?: ProgressMap;
  /** ADR-0152 思考分頻：SSE delta.reasoning_content 的累積思考內容。
   *  隨樹進 session 持久化（後端 MessageNode extra:allow 原樣收存），
   *  MessageItem 以摺疊區塊顯示（預設收合）。 */
  thinking?: string;
  /** ADR-0249：這一回合中已送達的插話（第 N 步）；不算回合，組歷史時加成標註。 */
  interjections?: Interjection[];
  /** ADR-0249 第 4b 段：這一回合被 Ctrl+Enter 打斷的那幾輪（第 N 步）。 */
  interruptions?: RoundInterruption[];
  /** ADR-0292：這則 user 訊息被 UserPromptSubmit hook 擋下；之後組歷史時它和後面的回覆都不送。 */
  hookBlocked?: boolean;
}

export interface TreeHistory {
  messages: Record<string, MessageNode>;
  currentId: string | null;
}

export const getLinearMessages = (hist: TreeHistory): MessageNode[] => {
  if (!hist || !hist.currentId || !hist.messages) return [];
  const linear: MessageNode[] = [];
  // 環路防禦：記錄已訪節點，遇到損毀樹（parent/children 形成迴圈）時安全中止，避免無限迴圈卡死 UI。
  const visited = new Set<string>();

  // 1. 向上尋找所有祖先 (到 Root)
  let currUp: string | null = hist.currentId;
  while (currUp && hist.messages[currUp]) {
    if (visited.has(currUp)) break; // parent 鏈成環 → 中止
    visited.add(currUp);
    linear.unshift(hist.messages[currUp]);
    currUp = hist.messages[currUp].parentId;
  }

  // 2. 向下尋找預設路徑 (到 Leaf)
  let currDown: string | null = hist.currentId;
  // currentId 已於上行加入 visited，故此處檢查「下一個」子節點以防環路（檢查 currDown 會誤砍正常路徑）。
  while (currDown && hist.messages[currDown]?.childrenIds?.length > 0) {
    const nextId: string = hist.messages[currDown].childrenIds[0];
    if (visited.has(nextId)) break; // children 鏈指回已訪節點 → 中止
    const nextMsg = hist.messages[nextId];
    if (nextMsg) {
      visited.add(nextId);
      linear.push(nextMsg);
      currDown = nextId;
    } else {
      break;
    }
  }

  return linear;
};

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export const migrateFlatToTree = (flatOrTree: any): TreeHistory => {
  if (!flatOrTree) return { messages: {}, currentId: null };
  
  // 如果已經是樹狀結構且包含 messages 物件
  if (!Array.isArray(flatOrTree) && typeof flatOrTree === 'object') {
    const rawMessages = flatOrTree.messages || flatOrTree;
    const currentId = flatOrTree.currentId || null;

    // 如果內部 messages 是字典格式 (Open WebUI 格式)
    if (typeof rawMessages === 'object' && !Array.isArray(rawMessages)) {
      const history: Record<string, MessageNode> = {};
      const keys = Object.keys(rawMessages);
      
      keys.forEach(id => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const msg: any = rawMessages[id];
        history[id] = {
          ...msg,
          id: msg.id || id,
          parentId: msg.parentId || null,
          childrenIds: msg.childrenIds || []
        };
      });

      // 再次確保 childrenIds 雙向關聯正確 (有些匯出檔只有 parentId)
      keys.forEach(id => {
        const msg = history[id];
        if (msg.parentId && history[msg.parentId]) {
          if (!history[msg.parentId].childrenIds.includes(id)) {
            history[msg.parentId].childrenIds.push(id);
          }
        }
      });

      let finalCurrentId = currentId;
      if (!finalCurrentId && keys.length > 0) {
        // 找到沒有子節點的最後一個作為預設
        const leaf = keys.reverse().find(k => history[k].childrenIds.length === 0);
        finalCurrentId = leaf || keys[0];
      }

      return { ...(flatOrTree.messages ? flatOrTree : {}), messages: history, currentId: finalCurrentId };
    }
  }

  // 處理扁平陣列格式
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const messagesArray: any[] = Array.isArray(flatOrTree) ? flatOrTree : [];
  const history: Record<string, MessageNode> = {};

  // 1. 先為所有沒有 id 的訊息生成 id，並建立映射
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const processedArray = messagesArray.map((msg: any) => {
    return {
      ...msg,
      id: msg.id || crypto.randomUUID()
    };
  });

  // 2. 遍歷並建立 parentId (如果原本就沒有 parentId)
  for (let i = 0; i < processedArray.length; i++) {
    const msg = processedArray[i];
    const parentId = msg.parentId || (i > 0 ? processedArray[i - 1].id : null);
    history[msg.id] = {
      ...msg,
      parentId,
      childrenIds: []
    };
  }

  // 3. 建立 childrenIds 雙向連結
  Object.keys(history).forEach(id => {
    const msg = history[id];
    if (msg.parentId && history[msg.parentId]) {
      if (!history[msg.parentId].childrenIds.includes(id)) {
        history[msg.parentId].childrenIds.push(id);
      }
    }
  });

  // 4. 計算 leafNodes 與 currentId
  const leafNodes = Object.keys(history).filter(id => history[id].childrenIds.length === 0);
  let currentId: string | null = null;
  if (leafNodes.length > 0) {
    currentId = leafNodes[leafNodes.length - 1];
  } else if (processedArray.length > 0) {
    currentId = processedArray[processedArray.length - 1].id;
  }

  return { messages: history, currentId };
};
