import { describe, it, expect, afterEach } from 'vitest';
import { formatToTaipeiTime, setTimeFormat12h } from '../time_utils';

// 固定 epoch（下午時段），用於驗證 12/24 制差異。
const EPOCH = 1_735_968_300_000;

describe('formatToTaipeiTime 12/24 小時制偏好（外觀打磨包）', () => {
  // 全域偏好為共享模組狀態；每測還原預設 12 制，避免污染其他用到時間格式的測試（sequence.shuffle）。
  afterEach(() => setTimeFormat12h(true));

  it('預設 12 小時制 → 含 上午/下午', () => {
    setTimeFormat12h(true);
    const out = formatToTaipeiTime(EPOCH, { hour: 'numeric', minute: 'numeric' });
    expect(out).toMatch(/上午|下午/);
  });

  it('設 24 小時制 → 不含 上午/下午', () => {
    setTimeFormat12h(false);
    const out = formatToTaipeiTime(EPOCH, { hour: 'numeric', minute: 'numeric' });
    expect(out).not.toMatch(/上午|下午/);
  });

  it('呼叫端顯式 hour12 優先於全域偏好', () => {
    setTimeFormat12h(false); // 全域 24 制
    const out = formatToTaipeiTime(EPOCH, { hour: 'numeric', minute: 'numeric', hour12: true });
    expect(out).toMatch(/上午|下午/); // 顯式 12 制勝出
  });

  it('純日期格式不受時間制影響（無時、分欄位）', () => {
    setTimeFormat12h(false);
    const out = formatToTaipeiTime(EPOCH, { year: 'numeric', month: 'numeric', day: 'numeric' });
    expect(out).not.toMatch(/上午|下午|:/);
  });
});
