import type { Session } from './types';
import type { MessageNode, TreeHistory } from './tree_history';
import { matchesSessionContent, sessionContentKey } from './session_persistence';

/**
 * 存檔因版本舊被擋時，看是不是聊天側剪裁造成的；是就以剪過的存檔為底，
 * 把這一頁新增或改過、而且接得上的部分接上去（ADR-0302）。不是就回 null，照舊提示。
 *
 * 剪裁（後端 `_rebuild_tree`）只留主幹的後段：第一則沒有 parentId、每則只連下一則、
 * 主幹外的分支丟掉、換上新摘要。逾時那條路剪的是文字版，存檔最後多一則這一輪的回覆。
 * 剪掉的前段、丟掉的分支與摘要一律以存檔為準，不寫回去。
 */
export function graftOntoTrim(stored: Session, base: Session, local: Session): Session | null {
  const storedTree = stored.messages as TreeHistory;
  const baseTree = base.messages as TreeHistory;
  const localTree = local.messages as TreeHistory;
  if (!isTrimOf(stored, base, localTree)) return null;
  const nodes = structuredClone(storedTree.messages);
  const changed = Object.values(localTree.messages).filter(node => !sameNode(node, baseTree.messages[node.id]));
  for (const node of changed) {
    const kept = nodes[node.id];
    if (kept) nodes[node.id] = { ...node, parentId: kept.parentId, childrenIds: kept.childrenIds };
    else if (baseTree.messages[node.id]) return null; // 剪掉或丟掉的那則本機又改過
  }
  // 新的一則要接得上：祖先還在存檔裡，一層一層接
  let added = changed.filter(node => !nodes[node.id]);
  while (added.length) {
    const ready = added.filter(node => node.parentId && nodes[node.parentId]);
    if (!ready.length) return null; // 接在被剪範圍或主幹外的分支上
    for (const node of ready) {
      nodes[node.id] = { ...node, childrenIds: [] };
      const parent = nodes[node.parentId!];
      if (!parent.childrenIds.includes(node.id)) parent.childrenIds = [...parent.childrenIds, node.id];
    }
    added = added.filter(node => !nodes[node.id]);
  }
  const currentId = localTree.currentId;
  if (!currentId || !nodes[currentId]) return null;
  return { ...local, messages: { messages: nodes, currentId }, context_summary: stored.context_summary };
}

// 和後端一樣，從 currentId 往上找到根
function trunk(tree: TreeHistory): MessageNode[] {
  const path: MessageNode[] = [];
  for (let id = tree.currentId; id && tree.messages[id] && !path.some(node => node.id === id); id = tree.messages[id].parentId) {
    path.unshift(tree.messages[id]);
  }
  return path;
}

const unlinked = (node: MessageNode) => sessionContentKey({ ...node, parentId: null, childrenIds: [] });
const sameNode = (node: MessageNode, other: MessageNode | undefined) =>
  other !== undefined && sessionContentKey(node) === sessionContentKey(other);

function isTrimOf(stored: Session, base: Session, localTree: TreeHistory): boolean {
  const storedTree = stored.messages as TreeHistory;
  const baseTree = base.messages as TreeHistory;
  const kept = trunk(storedTree);
  // 剪裁只改對話樹與摘要；其他欄位不同就不是它造成的。只比這一頁有的欄位：新建的對話少了後端補上的預設值
  const otherFields = { ...base, messages: undefined, updatedAt: undefined, context_summary: undefined } as unknown as Session;
  if (!kept.length || kept[0].parentId !== null || Object.keys(storedTree.messages).length !== kept.length ||
      !matchesSessionContent(otherFields, stored)) return false;
  const last = kept[kept.length - 1];
  const textReply = !baseTree.messages[last.id] && localTree.messages[last.id]?.role === 'assistant';
  const shared = textReply ? kept.slice(0, -1) : kept;
  const basePath = trunk(baseTree);
  const offset = basePath.length - shared.length;
  return shared.length > 0 && offset > 0 && shared.every((node, index) =>
    node.id === basePath[offset + index].id && unlinked(node) === unlinked(basePath[offset + index]));
}
