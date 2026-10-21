/**
 * Artifact JSX/TSX 轉譯（選項 A，ADR-0114）。
 *
 * 在「父層 ToukaChat」把使用者（AI）寫的 React/JSX/TSX 原始碼轉成純 JS **字串**，供
 * 沙箱 iframe 內執行。安全不變式（最重要）：本模組**只產生字串、絕不執行使用者碼**
 * （禁 eval / new Function）；實際執行只發生在 `sandbox="allow-scripts"`、無 same-origin、
 * `connect-src 'none'` 的 iframe 內（見 ReactRenderer）。
 *
 * 轉譯策略：preset-react（classic runtime → 產出 `React.createElement`，吃 iframe 注入的
 * 全域 `window.React`）＋ preset-typescript（剝型別）＋ transform-modules-commonjs
 * （`export default`→`exports.default`、`import`→`require()`；iframe 的 require shim 會對
 * 任何匯入丟出友善錯誤＝離線不支援外部匯入）。@babel/standalone 採 lazy import，僅在首次
 * 開啟 React artifact 時載入，不進初始 bundle。
 */

export type TranspileResult = { code: string } | { error: string };

// 上限：超大/病態 artifact 會讓主執行緒的 Babel 轉譯卡死父層 UI（ADR-0114 複審 DoS）。
// 256KB 對單檔元件綽綽有餘；超過即拒絕，不進 Babel。
const MAX_SOURCE_BYTES = 256 * 1024;

// @babel/standalone 型別在 ESM/CJS 互通下 default 包裝不一；以最小介面取用 transform。
interface BabelLike {
  transform: (code: string, opts: Record<string, unknown>) => { code?: string | null } | null;
}

let _babel: BabelLike | null = null;

async function getBabel(): Promise<BabelLike> {
  if (_babel) return _babel;
  const mod = (await import("@babel/standalone")) as unknown as BabelLike & { default?: BabelLike };
  // 兼容 ESM default 包裝與直接命名匯出。
  _babel = typeof mod.transform === "function" ? mod : (mod.default as BabelLike);
  return _babel;
}

/** 把 Babel 錯誤正規化為單行、可讀的訊息（去掉冗長 code frame 的多餘行）。 */
function normalizeError(err: unknown): string {
  const msg = err instanceof Error ? err.message : String(err);
  // Babel 的 message 常含 code frame（多行）；取首行即足以指出問題。
  return msg.split("\n")[0].trim() || "Transpile failed";
}

/**
 * 轉譯單檔 React/JSX/TSX 原始碼為可在 iframe 執行的 classic-script JS。
 *
 * 成功回 `{ code }`；語法錯誤等回 `{ error }`（**不 throw**，呼叫端據此顯示友善訊息）。
 * 保證：永不執行 `source`（只做字串轉換）。
 */
export async function transpileArtifact(source: string): Promise<TranspileResult> {
  if (source.length > MAX_SOURCE_BYTES) {
    return { error: `Artifact too large to preview (${source.length} > ${MAX_SOURCE_BYTES} chars).` };
  }
  try {
    const babel = await getBabel();
    const out = babel.transform(source, {
      filename: "artifact.tsx", // 讓 preset-typescript 以副檔名判定 TSX
      presets: [
        ["react", { runtime: "classic" }], // 產出 React.createElement（吃全域 window.React）
        "typescript",
      ],
      plugins: ["transform-modules-commonjs"], // export/import → exports/require（iframe shim 接手）
      sourceMaps: false,
      babelrc: false,
      configFile: false,
    });
    const code = out?.code;
    if (typeof code !== "string") {
      return { error: "Transpile produced no output" };
    }
    return { code };
  } catch (err) {
    return { error: normalizeError(err) };
  }
}
