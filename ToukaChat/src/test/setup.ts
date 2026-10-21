import '@testing-library/jest-dom';

// Node.js 22+ exposes a built-in localStorage that returns undefined unless
// --localstorage-file is passed. In jsdom test environments this prevents tests
// from accessing Storage APIs. Provide an explicit in-memory implementation that
// is always available regardless of runtime flags.
function makeStorage(): Storage {
  const store = new Map<string, string>();
  return {
    get length() { return store.size; },
    key: (n: number) => [...store.keys()][n] ?? null,
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => { store.set(k, String(v)); },
    removeItem: (k: string) => { store.delete(k); },
    clear: () => { store.clear(); },
  };
}

Object.defineProperty(globalThis, 'localStorage', {
  value: makeStorage(),
  writable: true,
  configurable: true,
});
Object.defineProperty(globalThis, 'sessionStorage', {
  value: makeStorage(),
  writable: true,
  configurable: true,
});

// i18n 測試語言固定為 zh-TW。Phase 1 起元件改用 react-i18next 的 t() 渲染字串，
// 既有測試以中文來源字串斷言（如 getByText('儲存配置')、name: /新增連線/i）。
// 固定 zh-TW 讓 t() 回傳來源中文，既有斷言無需改動即可通過。
// 以動態 import 確保上面的 in-memory localStorage 已就緒，i18n 初始化才不踩到
// Node 內建的非功能性 localStorage。
const { default: i18nForTests } = await import('@/lib/i18n');
await i18nForTests.changeLanguage('zh-TW');
