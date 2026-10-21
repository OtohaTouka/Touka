// src/lib/export-utils.ts
import { Session } from './types';
import { migrateFlatToTree, getLinearMessages, MessageNode } from './tree_history';
import { getSystemEpoch, toEpochMs } from './time_utils';

/**
 * 計算線性訊息序列中完整的來回數（SSOT）。
 * 定義：user + 其後緊接的 assistant = 1 round。
 * - 跳過 system/nudge 等非對話角色
 * - 末尾無 assistant 配對的 user 不計入
 */
function countRounds(messages: MessageNode[]): number {
    let rounds = 0;
    let i = 0;
    while (i < messages.length) {
        if (messages[i].role === 'user') {
            let j = i + 1;
            while (j < messages.length &&
                   messages[j].role !== 'user' && messages[j].role !== 'assistant') {
                j++;
            }
            if (j < messages.length && messages[j].role === 'assistant') {
                rounds++;
                i = j + 1;
            } else {
                i++;
            }
        } else {
            i++;
        }
    }
    return rounds;
}

/**
 * 以來回為單位分割訊息序列。
 * 分割點保證落在完整 pair 邊界，不拆斷 user+assistant 對。
 */
function splitByRounds(
    messages: MessageNode[],
    keepRounds: number,
): { legacy: MessageNode[]; kept: MessageNode[] } {
    const pairs: [number, number][] = [];
    let i = 0;
    while (i < messages.length) {
        if (messages[i].role === 'user') {
            let j = i + 1;
            while (j < messages.length &&
                   messages[j].role !== 'user' && messages[j].role !== 'assistant') {
                j++;
            }
            if (j < messages.length && messages[j].role === 'assistant') {
                pairs.push([i, j]);
                i = j + 1;
            } else {
                i++;
            }
        } else {
            i++;
        }
    }
    if (pairs.length <= keepRounds) {
        return { legacy: [], kept: messages };
    }
    const splitIdx = pairs[pairs.length - keepRounds][0];
    return { legacy: messages.slice(0, splitIdx), kept: messages.slice(splitIdx) };
}

/**
 * 切分 Session 的訊息（keepCount 以來回為單位）。
 */
export function splitSessionMessages(session: Session, keepCount: number = 30) {
    const tree = migrateFlatToTree(session.messages);
    const linearMessages = getLinearMessages(tree);

    if (countRounds(linearMessages) <= keepCount) {
        return null;
    }

    const { legacy: legacyMessages, kept: keepMessages } = splitByRounds(linearMessages, keepCount);
    const legacyUserCount = legacyMessages.filter(m => m.role === 'user').length;

    // [結構加固] 重定錨：將保留下來的第一則訊息的 parentId 設為 null，使其成為新樹的根節點
    if (keepMessages.length > 0) {
        keepMessages[0] = {
            ...keepMessages[0],
            parentId: null
        };
    }

    const legacyCount = legacyMessages.length;
    return { legacyCount, legacyMessages, keepMessages, legacyUserCount };
}

/** 訊息實際時間（Open WebUI 用秒）：timestamp 秒或毫秒都認；沒有就照舊用 createdAt（毫秒）或匯出當下。 */
const messageSeconds = (msg: Pick<MessageNode, 'timestamp' | 'createdAt'> | undefined): number =>
    Math.floor((toEpochMs(msg?.timestamp) ?? (msg?.createdAt || getSystemEpoch())) / 1000);

/**
 * 將 Touka 的會話與訊息轉換成 Open WebUI 的全量匯出格式 (.json)
 */
export function formatToOpenWebUI(session: Session, selectedModel: string) {
    const tree = migrateFlatToTree(session.messages);
    const messages = Object.values(tree.messages);
    const targetModel = session.agent_id || session.home_for_agent || selectedModel || "ToukaGroup";
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const messageRecord: Record<string, any> = {};

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    messages.forEach((msg: any) => {
        messageRecord[msg.id] = {
            id: msg.id,
            parentId: msg.parentId,
            childrenIds: msg.childrenIds || [],
            role: msg.role,
            content: msg.content,
            timestamp: messageSeconds(msg),
            models: [targetModel]
        };
    });

    return {
        id: session.id,
        user_id: "touka-user",
        title: session.title,
        chat: {
            id: session.id,
            title: session.title,
            models: [targetModel],
            params: {},
            history: {
                messages: messageRecord
            }
        },
        updated_at: Math.floor((session.updatedAt || getSystemEpoch()) / 1000),
        created_at: messageSeconds(messages[0])
    };
}

/**
 * 觸發瀏覽器下載 JSON 檔案
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function triggerDownload(content: any, filename: string) {
    const blob = new Blob([JSON.stringify(content, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}
