/**
 * 回覆失敗時講原因（路線圖第 8 項 PR 1）。
 * 認不得的代碼、舊對話沒有原因：顯示原本那一句，不顯示空白或代碼本身。
 */
import { afterEach, describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { GenerationFailedNotice } from '../GenerationFailedNotice';
import { GENERATION_FAILURE_KIND } from '@/hooks/useProgress';
import { useFailureDetails } from '@/lib/chat/failureDetailStore';

const failure = (error_type?: string) => ({ kind: GENERATION_FAILURE_KIND, error_type });
const remember = (detail: string) => useFailureDetails.setState({ details: { m1: detail } });

describe('GenerationFailedNotice', () => {
  afterEach(() => useFailureDetails.setState({ details: {} }));

  it.each([
    ['authentication_failed', '金鑰或權限'],
    ['rate_limit', '請求太多或額度用完'],
    ['billing_error', '帳務'],
    ['overloaded', '廠商目前很忙'],
    ['server_error', '廠商那邊出錯'],
    ['model_not_found', '可能已下架或名稱不對'],
    ['invalid_request', '廠商拒絕了這次請求'],
  ])('%s 講得出原因', (errorType, text) => {
    render(<GenerationFailedNotice messageId="m1" failure={failure(errorType)} />);
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('這一輪回覆失敗');
    expect(alert).toHaveTextContent(text);
  });

  it.each([[undefined], ['unknown'], ['some_new_code'], ['__proto__'], ['constructor']])(
    '認不得的代碼 %s 顯示原本那一句', (errorType) => {
      render(<GenerationFailedNotice messageId="m1" failure={failure(errorType)} />);
      expect(screen.getByRole('alert').textContent).toBe('這一輪回覆失敗，沒有完成。');
    });

  it('舊對話沒有原因：顯示原本那一句', () => {
    render(<GenerationFailedNotice messageId="m1" />);
    expect(screen.getByRole('alert').textContent).toBe('這一輪回覆失敗，沒有完成。');
  });

  it('有廠商原文就一起顯示', () => {
    remember('Model Not Exist');
    render(<GenerationFailedNotice messageId="m1" failure={failure('invalid_request')} />);
    expect(screen.getByRole('alert')).toHaveTextContent('Model Not Exist');
  });

  it('別則訊息的原文不會顯示在這一則', () => {
    remember('Model Not Exist');
    render(<GenerationFailedNotice messageId="m2" failure={failure('invalid_request')} />);
    expect(screen.getByRole('alert')).not.toHaveTextContent('Model Not Exist');
  });

  it('廠商原文當純文字：HTML 不會變成元素，Markdown 不會被解讀', () => {
    remember('<img src=x onerror=1> **bold** [a](http://x)');
    const { container } = render(<GenerationFailedNotice messageId="m1" failure={failure('invalid_request')} />);
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('strong')).toBeNull();
    expect(container.querySelector('a')).toBeNull();
    expect(screen.getByRole('alert')).toHaveTextContent('<img src=x onerror=1> **bold** [a](http://x)');
  });
});
