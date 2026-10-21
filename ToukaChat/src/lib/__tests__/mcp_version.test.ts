import { describe, it, expect } from 'vitest';
import {
  buildInstalledEntry,
  computeMcpStatus,
  formatHubDownloads,
  hubEntryConfigKey,
  InstalledMcpConfig,
  McpHubServerView,
} from '../mcp_version';

// F6（ADR-0190）：install 落盤鍵＝後端 derive_server_key(name)（如 'fetch'），
// hub.id 帶前綴（'official-fetch'）永遠對不上 config 鍵 → 徽章恆滅。
// 修法＝以後端投影的 server_key 查表；id 僅作無 server_key 舊 payload 的後備。
const HUB: McpHubServerView = {
  id: 'official-fetch',
  name: 'Fetch',
  server_key: 'fetch',
  description: 'd',
  command: 'uvx',
  args: ['mcp-server-fetch'],
};

// ADR-0192：remote 條目以 url 取代 command/args，比對鍵改看端點。
const REMOTE_HUB: McpHubServerView = {
  id: 'registry-acme/hosted',
  name: 'Acme Hosted',
  server_key: 'acme-hosted',
  description: 'd',
  command: undefined,
  args: [],
  url: 'https://acme.example/mcp',
};

describe('hubEntryConfigKey／buildInstalledEntry（ADR-0192 補齊 ADR-0190 的寫入側）', () => {
  it('安裝寫入鍵＝徽章查表鍵（server_key 優先，id 為舊 payload 後備）', () => {
    expect(hubEntryConfigKey(HUB)).toBe('fetch');
    expect(hubEntryConfigKey({ ...HUB, server_key: undefined })).toBe('official-fetch');
  });

  it('裝完立刻算狀態＝up_to_date（寫入鍵與查表鍵同源的端到端證明）', () => {
    // 這條是本修復的核心迴歸鎖：寫入用 id、查表用 server_key 的舊行為會讓
    // 剛安裝完的條目仍顯示「未安裝」。
    const cfg: InstalledMcpConfig = {
      [hubEntryConfigKey(HUB)]: buildInstalledEntry(HUB, {}, undefined),
    };
    expect(computeMcpStatus(HUB, cfg)).toBe('up_to_date');
  });

  it('更新時保留既有 env、僅以新輸入覆蓋同名鍵', () => {
    const existing = { command: 'uvx', args: ['mcp-server-fetch'], env: { OLD: 'keep', DUP: 'stale' } };
    const entry = buildInstalledEntry(HUB, { DUP: 'fresh' }, existing);
    expect(entry.env).toEqual({ OLD: 'keep', DUP: 'fresh' });
  });

  it('remote 條目寫 url＋header 模板，不寫 command', () => {
    const entry = buildInstalledEntry(REMOTE_HUB, { MCP_AUTHORIZATION: 'Bearer x' }, undefined);
    expect(entry.url).toBe('https://acme.example/mcp');
    expect(entry.command).toBeUndefined();
    expect(entry.env).toEqual({ MCP_AUTHORIZATION: 'Bearer x' });
  });

  it('Hub「更新設定」不得抹掉工具治理政策（ADR-0195 軸 A）', () => {
    // 前端 Hub 安裝**不打 /hub/install**，是客戶端組完條目後走 /ui/config；
    // 本函式重建整個條目，不帶回＝按一次「更新」就把 create_refund 重新打開。
    const existing = {
      command: 'uvx',
      args: ['mcp-server-fetch'],
      enabled_tools: ['fetch'],
      disabled_tools: ['dangerous_fetch'],
    };
    const entry = buildInstalledEntry(HUB, {}, existing);
    expect(entry.enabled_tools).toEqual(['fetch']);
    expect(entry.disabled_tools).toEqual(['dangerous_fetch']);
  });

  it('勾掉一個工具不得讓條目顯示成「可更新」（治理欄位不進 recipe 比對）', () => {
    const cfg: InstalledMcpConfig = {
      [hubEntryConfigKey(HUB)]: {
        ...buildInstalledEntry(HUB, {}, undefined),
        enabled_tools: ['fetch'],
      },
    };
    expect(computeMcpStatus(HUB, cfg)).toBe('up_to_date');
  });
});

describe('computeMcpStatus — remote 條目（ADR-0192）', () => {
  it('已裝且端點一致 → up_to_date', () => {
    const cfg: InstalledMcpConfig = {
      'acme-hosted': { url: 'https://acme.example/mcp' },
    };
    expect(computeMcpStatus(REMOTE_HUB, cfg)).toBe('up_to_date');
  });

  it('策展換了端點 → update_available（remote 無版本號，端點就是配方）', () => {
    const cfg: InstalledMcpConfig = {
      'acme-hosted': { url: 'https://old.example/mcp' },
    };
    expect(computeMcpStatus(REMOTE_HUB, cfg)).toBe('update_available');
  });

  it('未安裝 → not_installed', () => {
    expect(computeMcpStatus(REMOTE_HUB, {})).toBe('not_installed');
  });
});

describe('computeMcpStatus（真實形狀：config 鍵＝derive_server_key(name)，非 hub.id）', () => {
  it('未安裝 → not_installed', () => {
    expect(computeMcpStatus(HUB, {})).toBe('not_installed');
  });

  it('已裝（鍵=server_key）且 command+args 一致 → up_to_date', () => {
    const cfg: InstalledMcpConfig = {
      fetch: { command: 'uvx', args: ['mcp-server-fetch'] },
    };
    expect(computeMcpStatus(HUB, cfg)).toBe('up_to_date');
  });

  it('已裝但 args 不同 → update_available', () => {
    const cfg: InstalledMcpConfig = {
      fetch: { command: 'uvx', args: ['mcp-server-fetch@0.1'] },
    };
    expect(computeMcpStatus(HUB, cfg)).toBe('update_available');
  });

  it('已裝但 command 不同 → update_available', () => {
    const cfg: InstalledMcpConfig = {
      fetch: { command: 'npx', args: ['mcp-server-fetch'] },
    };
    expect(computeMcpStatus(HUB, cfg)).toBe('update_available');
  });

  it('無 server_key 的舊 payload → 後備以 id 查表（維持相容、不誤報未裝）', () => {
    const legacy: McpHubServerView = { ...HUB, server_key: undefined };
    const cfg: InstalledMcpConfig = {
      'official-fetch': { command: 'uvx', args: ['mcp-server-fetch'] },
    };
    expect(computeMcpStatus(legacy, cfg)).toBe('up_to_date');
  });
});

describe('computeMcpStatus 哨兵位通配（install 落盤＝解析後絕對路徑，ADR-0186）', () => {
  const FS_HUB: McpHubServerView = {
    id: 'official-filesystem',
    name: 'Filesystem',
    server_key: 'filesystem',
    description: 'd',
    command: 'npx',
    args: ['-y', '@modelcontextprotocol/server-filesystem', '@TOUKA_APPDATA@'],
  };

  it('哨兵位對到已解析絕對路徑 → up_to_date（不得假陽性「可更新」）', () => {
    const cfg: InstalledMcpConfig = {
      filesystem: {
        command: 'npx',
        args: ['-y', '@modelcontextprotocol/server-filesystem', 'C:\\Users\\x\\app_data'],
      },
    };
    expect(computeMcpStatus(FS_HUB, cfg)).toBe('up_to_date');
  });

  it('非哨兵位漂移仍 → update_available（通配僅限哨兵位）', () => {
    const cfg: InstalledMcpConfig = {
      filesystem: {
        command: 'npx',
        args: ['-y', '@modelcontextprotocol/server-filesystem@0.1', 'C:\\Users\\x\\app_data'],
      },
    };
    expect(computeMcpStatus(FS_HUB, cfg)).toBe('update_available');
  });

  it('引數長度不符（哨兵位整個缺席）→ update_available', () => {
    const cfg: InstalledMcpConfig = {
      filesystem: { command: 'npx', args: ['-y', '@modelcontextprotocol/server-filesystem'] },
    };
    expect(computeMcpStatus(FS_HUB, cfg)).toBe('update_available');
  });
});

describe('formatHubDownloads（npm 下載量誠實顯示）', () => {
  it('無數據（undefined/null）→「精選」，不偽造數字', () => {
    expect(formatHubDownloads(undefined)).toBe('精選');
    expect(formatHubDownloads(null)).toBe('精選');
  });

  it('真 0 → 顯示 "0"（誠實呈現 API 回傳的零）', () => {
    expect(formatHubDownloads(0)).toBe('0');
  });

  it('正數 → 千分位縮寫', () => {
    expect(formatHubDownloads(1234)).toBe('1.2k');
    expect(formatHubDownloads(50000)).toBe('50.0k');
  });
});
