import { describe, expect, it } from 'vitest';
import {
    catalogFromToolsStats,
    impliedTools,
    isToolSelected,
    shortNames,
    toggleTool,
    unlistedEntries,
} from '../soul_tools';

// ADR-0281 T2：靈魂工具清單存 `伺服器/工具`；舊長名照舊算勾了；外掛不列。
const STATS = {
    builtin_schemas: [{ function: { name: 'read', description: '讀檔' } }],
    memory_schemas: [{ function: { name: 'memory_tools', description: '記憶' } }],
    plugin_schemas: [{ function: { name: 'my_plugin', description: '外掛' } }],
    mcp_schemas: [
        { function: { name: 'lookup', raw_name: 'srv__lookup', description: '查' }, server: 'srv', tool: 'lookup' },
        { function: { name: 'lookup', raw_name: 'other__lookup' }, server: 'other', tool: 'lookup' },
        { function: { name: 'legacy', raw_name: 'old__legacy' } },
        // 名稱含斜線的存不成恰好一個斜線的 `伺服器/工具`，不列
        { function: { name: 'a/b', raw_name: 'srv__a/b' }, server: 'srv', tool: 'a/b' },
    ],
    tool_companions: { execute_command: ['get_command_result', 'stop_command'], broken: 'x' },
};

describe('catalogFromToolsStats', () => {
    it('keys MCP tools as server/tool, keeps builtin names and drops plugins', () => {
        const catalog = catalogFromToolsStats(STATS);
        expect(catalog.status).toBe('ready');
        expect(catalog.tools.map(entry => entry.key)).toEqual(['read', 'memory_tools', 'srv/lookup', 'other/lookup']);
        expect(catalog.tools.find(entry => entry.key === 'srv/lookup')).toMatchObject({
            label: 'srv/lookup', description: '查', legacyKey: 'srv__lookup',
        });
        expect(catalog.companions).toEqual({ execute_command: ['get_command_result', 'stop_command'] });
    });

    it('tolerates a missing or partial response', () => {
        expect(catalogFromToolsStats({})).toEqual({ status: 'ready', tools: [], companions: {} });
        expect(catalogFromToolsStats({ builtin_schemas: [{}, { function: { name: '' } }] }).tools).toEqual([]);
    });
});

describe('selection helpers', () => {
    const { tools: catalog } = catalogFromToolsStats(STATS);
    const srv = catalog.find(entry => entry.key === 'srv/lookup')!;
    const read = catalog.find(entry => entry.key === 'read')!;

    it('counts the new key and the legacy long name as selected', () => {
        expect(isToolSelected(srv, ['srv/lookup'])).toBe(true);
        expect(isToolSelected(srv, ['srv__lookup'])).toBe(true);
        expect(isToolSelected(srv, ['lookup'])).toBe(false);
        expect(isToolSelected(srv, ['other/lookup'])).toBe(false);
    });

    it('adds the new key and removes both forms when unticked', () => {
        expect(toggleTool(srv, ['read'])).toEqual(['read', 'srv/lookup']);
        expect(toggleTool(srv, ['read', 'srv__lookup', 'srv/lookup'])).toEqual(['read']);
        expect(toggleTool(read, ['read', 'gone/tool'])).toEqual(['gone/tool']);
    });

    it('keeps entries the catalog does not list and flags only plain unknown names', () => {
        const value = ['read', 'srv__lookup', 'gone/tool', 'old__legacy', 'lookup', 'search', 'lookup'];
        expect(unlistedEntries(value, catalog)).toEqual(['gone/tool', 'old__legacy', 'lookup', 'search']);
        expect(shortNames(value, catalog)).toEqual(['lookup', 'search']);
    });

    it('companions come only with a ticked tool that has them', () => {
        const companions = { execute_command: ['get_command_result', 'stop_command'] };
        expect([...impliedTools(['execute_command', 'read'], companions)]).toEqual(['get_command_result', 'stop_command']);
        expect(impliedTools(['bash', 'read'], companions).size).toBe(0);
        // 物件原型上的名稱不算配套
        expect(impliedTools(['constructor', 'toString', '__proto__'], companions).size).toBe(0);
    });
});
