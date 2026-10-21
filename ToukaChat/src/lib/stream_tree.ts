/**
 * ADR-0080 背景續跑：串流對話樹的純函式操作（不依賴 React）。
 *
 * 把「串流回調如何把 chunk / event 變成對話樹更新」抽離為純函式，
 * 供 StreamRegistryProvider 的 per-session 串流使用，並可獨立單測。
 *
 * SSOT：樹的變更邏輯集中於此，registry 只負責呼叫；progress 收斂沿用
 * 既有 SSOT `reduceProgress`（useProgress.ts），不另實作。
 */
import { TreeHistory, MessageNode, ToolStep, ToolStepApproval, ToolStepIsolation } from './tree_history';
import { reduceProgress } from '@/hooks/useProgress';
import type { ChatAttachment } from '@/lib/chat/attachment';
import type { Interjection } from '@/lib/types';
import { ROUND_INTERRUPTED_EVENT, parseRoundInterruptedEvent } from '@/lib/chat/interjections';
import { getSystemEpoch } from './time_utils';

export interface BuildStreamNodesParams {
  userText: string;
  attachment: ChatAttachment | null;
  parentId: string | null;
  /** 受控寫入重播時沿用同一操作建立的 ID。 */
  userMsgId?: string;
  /** 送出時間（毫秒）；同一次送出重算樹時沿用同一個值。預設現在。 */
  timestamp?: number;
}

export interface BuildStreamNodesResult {
  tree: TreeHistory;
  userMsgId: string;
  assistantMsgId: string;
}

/**
 * ADR-0081：在 baseTree 上只接出 user 回合（不含 assistant 佔位）。
 *
 * 此樹用於「送出時先行持久化」——重整後 resume 以它為重建基底，
 * 後端完成時的伺服器端 append 也以 currentId=user 為掛點。
 */
export function buildUserTurnTree(
  baseTree: TreeHistory,
  { userText, attachment, parentId, userMsgId = crypto.randomUUID(), timestamp = getSystemEpoch() }: BuildStreamNodesParams,
): { tree: TreeHistory; userMsgId: string } {
  if (parentId && !baseTree.messages[parentId]) throw new Error('Stream parent is no longer present');
  if (baseTree.messages[userMsgId]) throw new Error('Stream node already exists');
  const userMsg: MessageNode = {
    id: userMsgId,
    parentId,
    childrenIds: [],
    role: 'user',
    content: userText,
    // 保留全部欄位（name/text/size…）——text 附件需在送出時 fork 內嵌（不可只留 type/url）。
    attachment: attachment ? { ...attachment } : undefined,
    timestamp,
  };

  const messages: Record<string, MessageNode> = {
    ...baseTree.messages,
    [userMsgId]: userMsg,
  };
  // 維護分支父節點的 childrenIds（不可變更新）
  if (parentId && messages[parentId]) {
    messages[parentId] = {
      ...messages[parentId],
      childrenIds: [...messages[parentId].childrenIds, userMsgId],
    };
  }
  return { tree: { ...baseTree, messages, currentId: userMsgId }, userMsgId };
}

/**
 * ADR-0081：在 currentId 下接出待填的空 assistant 節點（resume 重建與發送共用）。
 */
export function appendAssistantNode(
  tree: TreeHistory,
  assistantMsgId = crypto.randomUUID(),
): { tree: TreeHistory; assistantMsgId: string } {
  const parentId = tree.currentId;
  const assistantMsg: MessageNode = {
    id: assistantMsgId,
    parentId,
    childrenIds: [],
    role: 'assistant',
    content: '',
  };
  const messages: Record<string, MessageNode> = {
    ...tree.messages,
    [assistantMsgId]: assistantMsg,
  };
  if (parentId && messages[parentId]) {
    messages[parentId] = {
      ...messages[parentId],
      childrenIds: [...messages[parentId].childrenIds, assistantMsgId],
    };
  }
  return { tree: { ...tree, messages, currentId: assistantMsgId }, assistantMsgId };
}

/** 蓋完成時間：節點已有時間（含後端寫的）就不動，節點不存在回原樹。 */
export function stampNodeTime(tree: TreeHistory, nodeId: string, at: number): TreeHistory {
  const node = tree.messages[nodeId];
  if (!node || node.timestamp !== undefined) return tree;
  return { ...tree, messages: { ...tree.messages, [nodeId]: { ...node, timestamp: at } } };
}

/** 重播只接回已授權的固定節點，不能把舊生成接到新的分支。 */
export function restoreAssistantNode(tree: TreeHistory, parentId: string, assistantId: string): TreeHistory {
  const parent = tree.messages[parentId];
  const assistant = tree.messages[assistantId];
  if (parent?.role !== 'user' || (tree.currentId !== parentId && tree.currentId !== assistantId) ||
      (assistant && (assistant.role !== 'assistant' || assistant.parentId !== parentId))) {
    throw new Error('Stream base no longer matches');
  }
  return assistant ? tree : appendAssistantNode({ ...tree, currentId: parentId }, assistantId).tree;
}

/**
 * 在 baseTree 上接出 user 訊息與待填的 assistant 節點，回傳新樹與兩節點 id。
 * 維護 parentId 下的 childrenIds 雙向連結（支援分支），currentId 指向 assistant。
 * （組合 buildUserTurnTree + appendAssistantNode，行為與 ADR-0080 原版一致。）
 */
export function buildStreamNodes(
  baseTree: TreeHistory,
  params: BuildStreamNodesParams,
): BuildStreamNodesResult {
  const { tree: userTree, userMsgId } = buildUserTurnTree(baseTree, params);
  const { tree, assistantMsgId } = appendAssistantNode(userTree);
  return { tree, userMsgId, assistantMsgId };
}

/**
 * 把累積文字寫入指定 assistant 節點的 content。
 * 防污染：目標節點不存在（已刪/已停）→ 原樹返回，不塞殘缺節點。
 */
export function applyChunkToTree(
  tree: TreeHistory,
  assistantMsgId: string,
  fullText: string,
): TreeHistory {
  const node = tree.messages[assistantMsgId];
  if (!node) return tree;
  return {
    ...tree,
    messages: { ...tree.messages, [assistantMsgId]: { ...node, content: fullText } },
  };
}

/**
 * 把累積思考文字寫入指定 assistant 節點的 thinking（ADR-0152 思考分頻）。
 * 與 applyChunkToTree 同構：收「全量累積字串」而非增量；目標節點不存在 → 原樹返回。
 */
export function applyThinkingToTree(
  tree: TreeHistory,
  assistantMsgId: string,
  fullThinking: string,
): TreeHistory {
  const node = tree.messages[assistantMsgId];
  if (!node) return tree;
  return {
    ...tree,
    messages: { ...tree.messages, [assistantMsgId]: { ...node, thinking: fullThinking } },
  };
}

/** ADR-0249：把已送達的插話寫進 assistant 節點 interjections[]（同 id 取代）；節點不存在 → 原樹返回。 */
export function recordInterjection(tree: TreeHistory, assistantMsgId: string, item: Interjection): TreeHistory {
  const node = tree.messages[assistantMsgId];
  if (!node) return tree;
  const interjections = [...(node.interjections ?? []).filter(i => i.id !== item.id), item];
  return { ...tree, messages: { ...tree.messages, [assistantMsgId]: { ...node, interjections } } };
}

const ISOLATION_LEVELS: readonly unknown[] = ['none', 'landlock', 'executor'];
const ISOLATION_FALLBACKS: readonly unknown[] = ['executor', 'landlock', 'direct'];
// 卡片文案要的事實：事件欄位 → 卡片欄位（只收字串，其他型別略過）
const ISOLATION_FACTS = [
  ['reason', 'reason'], ['reason_detail', 'reasonDetail'], ['net', 'net'],
  ['block_ports_issue', 'blockPortsIssue'], ['local_issue', 'localIssue'],
] as const;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

/**
 * iso-c2：審批事件的 `isolation`。沒帶（或 null）照舊；看不懂的層級當 none（同後端
 * normalize_isolation），none 卻沒有合法 fallback 時保留 null，卡片據此不讓核准。
 * features 收名稱清單或 `{name: true}`。
 */
function parseIsolation(raw: unknown): ToolStepIsolation | undefined {
  if (raw === undefined || raw === null) return undefined;
  const obj = isRecord(raw) ? raw : {};
  const level = ISOLATION_LEVELS.includes(obj.level) ? (obj.level as ToolStepIsolation['level']) : 'none';
  const fallback =
    level === 'none' && ISOLATION_FALLBACKS.includes(obj.fallback)
      ? (obj.fallback as ToolStepIsolation['fallback'])
      : null;
  const features = Array.isArray(obj.features)
    ? obj.features.filter((name): name is string => typeof name === 'string')
    : isRecord(obj.features)
      ? Object.keys(obj.features).filter(name => (obj.features as Record<string, unknown>)[name] === true)
      : [];
  const isolation: ToolStepIsolation = { level, fallback, features };
  if (ISOLATION_LEVELS.includes(obj.expected)) isolation.expected = obj.expected as ToolStepIsolation['level'];
  for (const [from, to] of ISOLATION_FACTS) {
    const value = obj[from];
    if (typeof value === 'string') isolation[to] = value;
  }
  // C2-3：後端只在未隔離卡不完整時送 false；有這個鍵卻不是 true 一律當不完整（卡片不讓核准）
  if (obj.complete !== undefined && obj.complete !== true) isolation.complete = false;
  return isolation;
}

/**
 * 把一筆結構化串流事件套用到指定 assistant 節點（steps / progress）。
 * 防污染：目標節點不存在 → 原樹返回。
 * 註：skill_install_approval_request 不在此處理（不更新樹），由 registry 以回調分發。
 */
export function applyEventToTree(
  tree: TreeHistory,
  assistantMsgId: string,
  event: Record<string, unknown>,
): TreeHistory {
  const node = tree.messages[assistantMsgId];
  if (!node) return tree;

  if (event.type === 'touka_progress') {
    const newProgress = reduceProgress(node.progress, event);
    return {
      ...tree,
      messages: { ...tree.messages, [assistantMsgId]: { ...node, progress: newProgress } },
    };
  }

  if (event.type === ROUND_INTERRUPTED_EVENT) {
    const interruption = parseRoundInterruptedEvent(event);
    if (!interruption) return tree;
    const interruptions = [...(node.interruptions ?? []), interruption];
    return { ...tree, messages: { ...tree.messages, [assistantMsgId]: { ...node, interruptions } } };
  }

  let newSteps: ToolStep[] = [...(node.steps || [])];
  if (event.type === 'tool_start') {
    // H3：若審批卡已為同一 call_id 建立步驟（awaiting_approval），核准後的
    // tool_start 就地升級為 running——不重複建行。
    const existing = newSteps.some(s => s.id === event.id);
    if (existing) {
      newSteps = newSteps.map(s =>
        s.id === event.id
          ? { ...s, status: 'running', args: (event.args as unknown) ?? s.args }
          : s,
      );
    } else {
      newSteps.push({
        id: (event.id as string) || '',
        tool: (event.tool as string) || '',
        args: event.args as unknown,
        status: 'running',
      });
    }
  } else if (event.type === 'tool_approval_request') {
    // H3：統一審批卡（hitl-unification H2 broker）——以 call_id 建 awaiting 步驟；
    // 事件 id＝exec_id（approve/reject 解算鍵）掛進 approval 附載。
    const isolation = parseIsolation(event.isolation);
    newSteps.push({
      id: (event.call_id as string) || '',
      tool: (event.tool as string) || '',
      args: event.args as unknown,
      status: 'awaiting_approval',
      approval: {
        execId: (event.id as string) || '',
        risk: event.risk as string | undefined,
        timeout: event.timeout as number | undefined,
        // ADR-0246：只在有值時帶；非預期型別一律不顯示
        ...(typeof event.review_reason === 'string' ? { reviewReason: event.review_reason } : {}),
        ...(event.human_only === true ? { humanOnly: true } : {}),
        ...(isolation ? { isolation } : {}),
      },
    });
  } else if (event.type === 'tool_approval_resolved') {
    // 解算（approved/rejected/timeout）：記進 approval.decision——卡片據此收斂；
    // 最終行狀態由後續 tool_start（核准）或 tool_result error（拒絕/逾時）決定。
    newSteps = newSteps.map(s =>
      s.id === event.call_id
        ? {
            ...s,
            approval: {
              ...s.approval,
              execId: (event.id as string) || s.approval?.execId || '',
              decision: event.decision as ToolStepApproval['decision'],
            },
          }
        : s,
    );
  } else if (event.type === 'mcp_interaction_request') {
    newSteps.push({
      id: (event.id as string) || '',
      tool: (event.tool as string) || '',
      args: null,
      status: 'awaiting_approval',
      interaction: {
        execId: (event.id as string) || '',
        kind: 'elicitation_form',
        message: (event.message as string) || '',
        schema: (event.schema as Record<string, unknown>) || {},
        timeout: event.timeout as number | undefined,
      },
    });
  } else if (event.type === 'mcp_interaction_resolved') {
    newSteps = newSteps.map(s =>
      s.id === event.id
        ? {
            ...s,
            status: event.decision === 'timeout' ? 'error' : 'success',
            interaction: s.interaction
              ? {
                  ...s.interaction,
                  decision: event.decision as
                    | 'accepted'
                    | 'declined'
                    | 'cancelled'
                    | 'timeout',
                }
              : undefined,
          }
        : s,
    );
  } else if (event.type === 'tool_result') {
    newSteps = newSteps.map(s =>
      s.id === event.id
        ? { ...s, status: event.status === 'error' ? 'error' : 'success', result: event.result }
        : s,
    );
  } else if (event.type === 'tool_stream') {
    newSteps = newSteps.map(s =>
      s.id === event.id
        ? { ...s, result: (typeof s.result === 'string' ? s.result : '') + ((event.chunk as string) || '') }
        : s,
    );
  } else if (event.type === 'error') {
    newSteps = newSteps.map(s =>
      s.id === event.id ? { ...s, status: 'error', result: event.message } : s,
    );
  } else {
    // 非樹更新類事件（如 skill_install_approval_request）：不動樹
    return tree;
  }

  return {
    ...tree,
    messages: { ...tree.messages, [assistantMsgId]: { ...node, steps: newSteps } },
  };
}
