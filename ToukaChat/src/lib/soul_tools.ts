// 靈魂工具清單（ADR-0281 T2）：`enabled_tools` 存什麼、怎麼算「勾了」。
// 比對規則以後端 `src/utils/soul_tool_selection.py` 為準（SSOT）；這裡只決定畫面存的格式：
// 內建與記憶工具存名稱，MCP 存 `伺服器/工具`；舊的長名 `伺服器__工具` 照舊算勾了。
// 外掛不列：外掛從不列給模型（決定 2 甲），勾了也叫不到。

/** 勾選清單裡的一個工具。 */
export interface SoulToolEntry {
    /** 存進 enabled_tools 的值 */
    key: string;
    label: string;
    description: string;
    /** MCP 的舊長名（廣告名）：舊設定存這個也算勾了 */
    legacyKey?: string;
}

interface ToolSchema {
    function?: { name?: string; raw_name?: string; description?: string };
    server?: string;
    tool?: string;
}

/** `/v1/tools_stats` 回應裡畫面用得到的部分。 */
export interface ToolsStats {
    builtin_schemas?: ToolSchema[];
    memory_schemas?: ToolSchema[];
    plugin_schemas?: ToolSchema[];
    mcp_schemas?: ToolSchema[];
    /** 勾了左邊就一起開的工具（後端 `TOOL_COMPANIONS`） */
    tool_companions?: Record<string, unknown>;
}

/** 勾選清單：讀完之前（loading／failed）不能拿來判斷「不在清單」。 */
export interface SoulToolCatalog {
    status: 'loading' | 'ready' | 'failed';
    tools: SoulToolEntry[];
    companions: Record<string, string[]>;
}

const MCP_SEPARATOR = '/';
const LEGACY_MCP_SEPARATOR = '__';

export function catalogFromToolsStats(stats: ToolsStats): SoulToolCatalog {
    return { status: 'ready', tools: toolEntries(stats), companions: companionMap(stats.tool_companions) };
}

function companionMap(raw: Record<string, unknown> | undefined): Record<string, string[]> {
    return Object.fromEntries(Object.entries(raw ?? {}).filter(
        (pair): pair is [string, string[]] => Array.isArray(pair[1]) && pair[1].every(name => typeof name === 'string'),
    ));
}

function toolEntries(stats: ToolsStats): SoulToolEntry[] {
    const entries: SoulToolEntry[] = [];
    for (const schema of [...(stats.builtin_schemas ?? []), ...(stats.memory_schemas ?? [])]) {
        const name = schema.function?.name;
        if (name) entries.push({ key: name, label: name, description: schema.function?.description ?? '' });
    }
    for (const schema of stats.mcp_schemas ?? []) {
        // 名稱含斜線的存不成恰好一個斜線的 `伺服器/工具`（後端不認），不列
        if (!schema.server || !schema.tool || `${schema.server}${schema.tool}`.includes(MCP_SEPARATOR)) continue;
        const key = `${schema.server}${MCP_SEPARATOR}${schema.tool}`;
        entries.push({
            key,
            label: key,
            description: schema.function?.description ?? '',
            legacyKey: schema.function?.raw_name,
        });
    }
    const seen = new Set<string>();
    return entries.filter(entry => {
        if (seen.has(entry.key)) return false;
        seen.add(entry.key);
        return true;
    });
}

function matches(entry: SoulToolEntry, item: string): boolean {
    return item === entry.key || (entry.legacyKey !== undefined && item === entry.legacyKey);
}

export function isToolSelected(entry: SoulToolEntry, value: readonly string[]): boolean {
    return value.some(item => matches(entry, item));
}

export function toggleTool(entry: SoulToolEntry, value: readonly string[]): string[] {
    return isToolSelected(entry, value)
        ? value.filter(item => !matches(entry, item))
        : [...value, entry.key];
}

/** 清單裡找不到的條目：存檔時照樣保留（斷線伺服器的工具、你個人授權的工具、舊短名）。 */
export function unlistedEntries(value: readonly string[], catalog: readonly SoulToolEntry[]): string[] {
    return [...new Set(value)].filter(item => !catalog.some(entry => matches(entry, item)));
}

/** 不生效的舊短名：找不到、又不是 `伺服器/工具` 或舊長名。 */
export function shortNames(value: readonly string[], catalog: readonly SoulToolEntry[]): string[] {
    return unlistedEntries(value, catalog).filter(
        item => !item.includes(MCP_SEPARATOR) && !item.includes(LEGACY_MCP_SEPARATOR),
    );
}

/** 因為勾了別的工具而一起開的工具（例如執行指令的取結果、停止指令）。 */
export function impliedTools(value: readonly string[], companions: Record<string, string[]>): Set<string> {
    return new Set(value.flatMap(item => companionsFor(companions, item)));
}

/** 一個工具的配套；只看自己的鍵（`constructor` 這類名稱在每個物件的原型上都有）。 */
export function companionsFor(companions: Record<string, string[]>, item: string): string[] {
    return Object.hasOwn(companions, item) ? companions[item] : [];
}
