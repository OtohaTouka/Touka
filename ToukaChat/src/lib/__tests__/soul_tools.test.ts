import { describe, expect, it } from 'vitest';
import {
    catalogFromToolsStats,
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
    ],
};

describe('catalogFromToolsStats', () => {
    it('keys MCP tools as server/tool, keeps builtin names and drops plugins', () => {
        const catalog = catalogFromToolsStats(STATS);
        expect(catalog.map(entry => entry.key)).toEqual(['read', 'memory_tools', 'srv/lookup', 'other/lookup']);
        expect(catalog.find(entry => entry.key === 'srv/lookup')).toMatchObject({
            label: 'srv/lookup', description: '查', legacyKey: 'srv__lookup',
        });
    });

    it('tolerates a missing or partial response', () => {
        expect(catalogFromToolsStats({})).toEqual([]);
        expect(catalogFromToolsStats({ builtin_schemas: [{}, { function: { name: '' } }] })).toEqual([]);
    });
});

describe('selection helpers', () => {
    const catalog = catalogFromToolsStats(STATS);
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
});
