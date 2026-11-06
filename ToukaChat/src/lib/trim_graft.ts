import type { Session } from './types';
import { migrateFlatToTree } from './tree_history';
import type { MessageNode, TreeHistory } from './tree_history';
import { matchesSessionContent, sessionContentKey } from './session_persistence';

/**
 * 存檔因版本舊被擋時，看是不是聊天側剪裁造成的；是就以剪過的存檔為底，
 * 把這一頁的改動接上去（ADR-0302）。不是、接不上或樹讀不懂就回 null，照舊提示。
 *
 * 剪裁（後端 `_rebuild_tree`）只留主幹的後段：第一則沒有 parentId、每則只連下一則、
 * 主幹外的分支丟掉、換上新摘要。逾時那條路剪的是文字版，存檔最後多一則這一輪的回覆。
 * 結果只含存檔留下、這一頁也還留著的節點，加上這一頁新增、接得上的節點：
 * 剪掉的前段、丟掉的分支與摘要以存檔為準，這一頁刪掉或切分掉的也不寫回去。
 */
export function graftOntoTrim(stored: Session, base: Session, local: Session): Session | null {
  try {
    return graft(stored, base, local);
  } catch {
    return null;
  }
}

function graft(stored: Session, base: Session, local: Session): Session | null {
  const [storedTree, baseTree, localTree] = [stored, base, local].map(treeOf);
  if (!isTrimOf(stored, storedTree, base, baseTree, localTree)) return null;
  const [storedNodes, baseNodes, localNodes] = [storedTree, baseTree, localTree].map(tree => tree.messages);
  // 剪掉或丟掉的那則本機又改過（含在底下接新的一則）：接上去會把剪掉的內容帶回來
  if (Object.values(localNodes).some(node =>
    baseNodes[node.id] && !storedNodes[node.id] && !sameNode(node, baseNodes[node.id]))) return null;
  const ids = new Set(Object.keys(storedNodes).filter(id => localNodes[id]));
  // 新的一則要接得上：祖先在結果裡，一層一層接
  let added = Object.values(localNodes).filter(node => !baseNodes[node.id] && !ids.has(node.id));
  while (added.length) {
    const ready = added.filter(node => node.parentId && ids.has(node.parentId));
    if (!ready.length) return null; // 接在被剪範圍或主幹外的分支上
    for (const node of ready) ids.add(node.id);
    added = added.filter(node => !ids.has(node.id));
  }
  const currentId = localTree.currentId;
  if (!currentId || !ids.has(currentId)) return null;
  const messages: Record<string, MessageNode> = {};
  for (const id of ids) {
    const node = localNodes[id];
    const parentId = node.parentId && ids.has(node.parentId) ? node.parentId : null; // 上一則被剪掉：這則成了第一則
    messages[id] = { ...node, parentId, childrenIds: node.childrenIds.filter(child => ids.has(child)) };
  }
  return { ...local, messages: { messages, currentId }, context_summary: stored.context_summary };
}

// 複製一份再正規化：舊格式的修補會改到傳進來的陣列
const treeOf = (session: Session): TreeHistory => migrateFlatToTree(structuredClone(session.messages));

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

function isTrimOf(stored: Session, storedTree: TreeHistory, base: Session, baseTree: TreeHistory, localTree: TreeHistory): boolean {
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
