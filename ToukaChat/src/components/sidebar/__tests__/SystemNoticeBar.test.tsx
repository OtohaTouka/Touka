import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { SystemNoticeBar } from '../SystemNoticeBar';
import { DegradedBanner } from '../DegradedBanner';
import zhTW from '@/locales/zh-TW/translation.json';
import en from '@/locales/en/translation.json';

// /health 的 system_notices（後端 observability.py 的 SYSTEM_NOTICE_*）：每個代碼都要說出原因與恢復方法
const CODES = ['autonomy_config_unreadable', 'auth_policies_unreadable'] as const;

const colorTokens = (element: HTMLElement) =>
  element.className.split(/\s+/).filter(token => token.startsWith('bg-') || token.startsWith('text-')).sort();
const messages = (catalog: unknown): Record<string, string> =>
  (catalog as { sidebar: { system_notice: Record<string, string> } }).sidebar.system_notice;

describe('SystemNoticeBar', () => {
  it.each(CODES)('shows %s as a translated alert as prominent as the degraded banner', code => {
    render(
      <>
        <DegradedBanner degraded reason="synthetic reason" />
        <SystemNoticeBar notices={[{ code, level: 'warning' }]} />
      </>,
    );
    const notice = screen.getByTestId('system-notice');
    expect(notice).toHaveAttribute('role', 'alert');
    expect(colorTokens(notice)).toEqual(colorTokens(screen.getByTestId('degraded-banner')));
    expect(notice.textContent).toBe(messages(zhTW)[code]);
  });

  it('says why autonomy stopped and how to bring it back', () => {
    const text = messages(zhTW).autonomy_config_unreadable;
    expect(text).toContain('自主設定（autonomy.json）讀不到，已暫停自主執行：工具與心跳都停了');
    expect(text).toContain('修好檔案權限後重新啟動 Touka');
  });

  it('warns not to save the security page while autonomy is unreadable', () => {
    // 安全設定頁顯示的是暫代的最嚴值，存檔會蓋掉原本的 autonomy.json（第 4 輪 A4）
    const zh = messages(zhTW).autonomy_config_unreadable;
    expect(zh).toContain('這時不要在安全設定頁按存檔');
    expect(zh).toContain('會用暫代的最嚴值蓋掉原本的設定');
    const english = messages(en).autonomy_config_unreadable;
    expect(english).toContain("Don't save anything on the Security settings page meanwhile");
    expect(english).toContain('overwrite your original settings with temporary strictest values');
  });

  it('says every tool and command is refused while the security rules are unreadable', () => {
    const text = messages(zhTW).auth_policies_unreadable;
    expect(text).toContain('auth_policies.json');
    expect(text).toContain('所有工具和指令都拒絕，代理人只能聊天');
    expect(text).toContain('重新啟動 Touka');
  });

  it.each(CODES)('has an English message for %s', code => {
    expect(messages(en)[code]?.length ?? 0).toBeGreaterThan(0);
  });

  it('renders nothing without notices or for codes it cannot describe', () => {
    const unknown = render(<SystemNoticeBar notices={[{ code: 'not_a_known_code', level: 'warning' }]} />);
    expect(unknown.container).toBeEmptyDOMElement();
    const empty = render(<SystemNoticeBar />);
    expect(empty.container).toBeEmptyDOMElement();
  });
});
