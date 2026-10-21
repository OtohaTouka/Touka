import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import path from 'path';
import { TEST_INCLUDE_GLOBS } from './scripts/lib/orphaned_tests_logic';

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    // SSOT：與 scripts/guard_orphaned_tests.ts 共用同一份 include globs。
    include: TEST_INCLUDE_GLOBS,
    testTimeout: 15000,
    pool: 'forks',
    maxWorkers: 4,
    // 隨機次序測試：打亂檔案與檔內測試順序，驗證測試隔離、揪出次序污染。
    // 重現某次失敗：vitest --sequence.seed=<N>（失敗輸出會印出 seed）。
    sequence: { shuffle: true },
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
});
