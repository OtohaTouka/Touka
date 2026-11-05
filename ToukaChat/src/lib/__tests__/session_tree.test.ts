import { describe, expect, it } from 'vitest';
import type { AgentInfo } from '@/lib/providers/AgentProvider';
import type { Session } from '@/lib/types';
import { buildSessionTree, canonicalSoulId, canSetHome, FOLDER_PREVIEW_COUNT, previewChildren } from '../session_tree';

function agent(id: string, type = 'soul', extra: Partial<AgentInfo> = {}): AgentInfo {
  return { id, name: id, type, is_group: type === 'group', ...extra };
}

function session(id: string, agentId: string | null, updatedAt: number, extra: Partial<Session> = {}): Session {
  return { id, title: id, messages: [], updatedAt, agent_id: agentId, ...extra };
}

const ids = (list: Session[]) => list.map(item => item.id);

describe('buildSessionTree', () => {
  it('每個靈魂一個資料夾：家對話在上面，其他對話新到舊', () => {
    const { folders, others } = buildSessionTree([
      session('old', 'Alice', 1),
      session('home', 'Alice', 2, { is_home: true }),
      session('new', 'Alice', 3),
    ], [agent('Alice')], 'ready');
    expect(others).toEqual([]);
    expect(folders).toHaveLength(1);
    expect(folders[0].name).toBe('Alice');
    expect(folders[0].home?.id).toBe('home');
    expect(ids(folders[0].children)).toEqual(['new', 'old']);
  });

  it('資料夾依底下最新的對話排序，家對話的時間也算', () => {
    const { folders } = buildSessionTree([
      session('a1', 'Alice', 10),
      session('b1', 'Bob', 5),
      session('b-home', 'Bob', 20, { is_home: true }),
      session('c1', 'Carol', 15),
    ], [agent('Alice'), agent('Bob'), agent('Carol')], 'ready');
    expect(folders.map(folder => folder.name)).toEqual(['Bob', 'Carol', 'Alice']);
  });

  it('不列空資料夾：一般帳號清單裡的佔位靈魂沒有對話就不出現', () => {
    const { folders } = buildSessionTree([session('a1', 'Alice', 1)],
      [agent('Alice'), agent('Ghost', 'soul_placeholder', { name: '佔位名稱' })], 'ready');
    expect(folders.map(folder => folder.name)).toEqual(['Alice']);
  });

  it('同一個靈魂的舊寫法都進同一個資料夾，顯示靈魂清單的名稱', () => {
    const { folders, others } = buildSessionTree([
      session('plain', 'Alice', 4),
      session('lower', 'alice', 3),
      session('colon', 'toukasoul:Alice', 2),
      session('prefix', 'ToukaSoulAlice', 1),
      session('lower-prefix', 'toukasoulalice', 0),
    ], [agent('Alice', 'soul', { name: '愛麗絲' })], 'ready');
    expect(others).toEqual([]);
    expect(folders).toHaveLength(1);
    expect(folders[0].name).toBe('愛麗絲');
    expect(folders[0].children).toHaveLength(5);
  });

  it('佔位靈魂有對話就開資料夾，顯示佔位名稱', () => {
    const { folders, others } = buildSessionTree([session('g', 'Ghost', 1)],
      [agent('Ghost', 'soul_placeholder', { name: '佔位名稱' })], 'ready');
    expect(others).toEqual([]);
    expect(folders.map(folder => folder.name)).toEqual(['佔位名稱']);
  });

  it('別名和正式 ID 是同一個資料夾', () => {
    const { folders } = buildSessionTree([session('by-alias', 'Ally', 2), session('plain', 'Alice', 1)],
      [agent('Alice', 'soul', { aliases: ['Ally'] })], 'ready');
    expect(folders.map(folder => ids(folder.children))).toEqual([['by-alias', 'plain']]);
  });

  it('別名算同一個靈魂，但不能蓋掉另一個靈魂的正式 ID', () => {
    const { folders } = buildSessionTree([session('by-alias', 'Ally', 2), session('bob', 'Bob', 1)],
      [agent('Alice', 'soul', { aliases: ['Ally', 'Bob'] }), agent('Bob')], 'ready');
    expect(folders.map(folder => [folder.name, ids(folder.children)])).toEqual([['Alice', ['by-alias']], ['Bob', ['bob']]]);
  });

  it('清單裡沒有的前綴名稱仍是靈魂：開資料夾，名稱去掉前綴', () => {
    const { folders } = buildSessionTree([session('s', 'ToukaSoulGhost', 1)], [], 'ready');
    expect(folders.map(folder => folder.name)).toEqual(['Ghost']);
  });

  it('只有前綴沒有名稱的不算靈魂', () => {
    const { folders, others } = buildSessionTree([session('s', 'ToukaSoul', 1)], [], 'ready');
    expect(folders).toEqual([]);
    expect(ids(others)).toEqual(['s']);
  });

  it('沒有靈魂的對話、自訂模型、群聊放其他對話，新到舊', () => {
    const { folders, others } = buildSessionTree([
      session('model', 'gpt-5', 1),
      session('custom', 'MyModel', 3),
      session('group', 'ToukaGroup', 2),
      session('none', null, 0),
    ], [agent('MyModel', 'custom'), agent('ToukaGroup', 'group')], 'ready');
    expect(folders).toEqual([]);
    expect(ids(others)).toEqual(['custom', 'group', 'model', 'none']);
  });

  it('清單讀到時，有家標記但不是靈魂的放其他對話', () => {
    const { folders, others } = buildSessionTree([session('s', 'gpt-5', 1, { is_home: true })], [agent('Alice')], 'ready');
    expect(folders).toEqual([]);
    expect(ids(others)).toEqual(['s']);
  });

  it('清單讀不到時，只靠前綴與家對話判斷，其餘放其他對話', () => {
    const { folders, others } = buildSessionTree([
      session('plain', 'Alice', 4),
      session('home', 'Alice', 3, { is_home: true }),
      session('prefix', 'ToukaSoulBob', 2),
    ], [], 'failed');
    expect(folders.map(folder => folder.name)).toEqual(['Alice', 'Bob']);
    expect(ids(others)).toEqual(['plain']);
  });

  it('有靈魂的無痕放進資料夾但不當家；沒有靈魂的放其他對話', () => {
    const { folders, others } = buildSessionTree([
      session('soul-incognito', 'Alice', 3, { incognito: true, is_home: true }),
      session('model-incognito', 'gpt-5', 2, { incognito: true }),
    ], [agent('Alice')], 'ready');
    expect(folders[0].home).toBeNull();
    expect(ids(folders[0].children)).toEqual(['soul-incognito']);
    expect(ids(others)).toEqual(['model-incognito']);
  });

  it('沒有 agent_id 時用 home_for_agent 找資料夾', () => {
    const { folders } = buildSessionTree([session('s', null, 1, { is_home: true, home_for_agent: 'Alice' })], [agent('Alice')], 'ready');
    expect(folders[0].home?.id).toBe('s');
  });

  it('同一個靈魂有兩個家標記時，新的當家，舊的留在子對話', () => {
    const { folders } = buildSessionTree([
      session('older', 'Alice', 1, { is_home: true }),
      session('newer', 'Alice', 2, { is_home: true }),
    ], [agent('Alice')], 'ready');
    expect(folders[0].home?.id).toBe('newer');
    expect(ids(folders[0].children)).toEqual(['older']);
  });
});

describe('canonicalSoulId', () => {
  it('只去掉大小寫完全相符的前綴，和後端相同', () => {
    expect(canonicalSoulId('ToukaSoulAlice')).toBe('Alice');
    expect(canonicalSoulId('Alice')).toBe('Alice');
    expect(canonicalSoulId('toukasoulAlice')).toBe('toukasoulAlice');
    expect(canonicalSoulId('toukasoul:Alice')).toBe('toukasoul:Alice');
  });
});

describe('canSetHome', () => {
  const agents = [agent('Alice'), agent('Ghost', 'soul_placeholder'), agent('MyModel', 'custom'), agent('ToukaGroup', 'group')];

  it('靈魂的對話可以；舊前綴換得成正式 ID 也可以', () => {
    expect(canSetHome(session('s', 'Alice', 1), agents)).toBe(true);
    expect(canSetHome(session('s', 'ToukaSoulAlice', 1), agents)).toBe(true);
  });

  it.each([
    ['佔位靈魂', 'Ghost'],
    ['自訂模型', 'MyModel'],
    ['群聊', 'ToukaGroup'],
    ['一般模型', 'gpt-5'],
    ['大小寫不同（後端會拒絕）', 'alice'],
    ['小寫前綴（後端會拒絕）', 'toukasoulAlice'],
    ['冒號格式（後端會拒絕）', 'toukasoul:Alice'],
  ])('%s不行', (_label, agentId) => {
    expect(canSetHome(session('s', agentId, 1), agents)).toBe(false);
  });

  it('沒有 agent_id、無痕都不行', () => {
    expect(canSetHome(session('s', null, 1), agents)).toBe(false);
    expect(canSetHome(session('s', 'Alice', 1, { incognito: true }), agents)).toBe(false);
  });
});

describe('previewChildren', () => {
  const children = Array.from({ length: FOLDER_PREVIEW_COUNT + 3 }, (_, index) => session(`s${index}`, 'Alice', 100 - index));

  it('先列 5 筆，其餘算在隱藏數量', () => {
    const { visible, hiddenCount } = previewChildren(children, false, new Set());
    expect(ids(visible)).toEqual(['s0', 's1', 's2', 's3', 's4']);
    expect(hiddenCount).toBe(3);
  });

  it('開著的對話不收起來，也不算在隱藏數量，順序不變', () => {
    const { visible, hiddenCount } = previewChildren(children, false, new Set(['s6']));
    expect(ids(visible)).toEqual(['s0', 's1', 's2', 's3', 's4', 's6']);
    expect(hiddenCount).toBe(2);
  });

  it('按過「還有 N 個」就全列', () => {
    const { visible, hiddenCount } = previewChildren(children, true, new Set());
    expect(visible).toHaveLength(children.length);
    expect(hiddenCount).toBe(0);
  });

  it('沒超過就沒有隱藏', () => {
    const { visible, hiddenCount } = previewChildren(children.slice(0, FOLDER_PREVIEW_COUNT), false, new Set());
    expect(visible).toHaveLength(FOLDER_PREVIEW_COUNT);
    expect(hiddenCount).toBe(0);
  });
});
