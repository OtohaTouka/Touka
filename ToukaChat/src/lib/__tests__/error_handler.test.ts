import { describe, it, expect } from 'vitest';
import { getCollisionMessage, getErrorMessage } from '../error_handler';

// C2（soul-crud repair）：ToukaException 的實際 wire 形狀是 FastAPI 再包一層 detail —
// {"detail": {"code","id","message","i18n"}}。修復前 getErrorMessage 只在頂層找 id，
// 巢內永遠匹配不到 SSOT → toast 直接吐 JSON.stringify(detail) 原始 JSON（使用者在
// SoulStudio 看到的就是這個）。本檔鎖住「巢狀 detail 也要對映到 SSOT/i18n」。

const toukaResponse = (code: string, id: number, message: string, i18nKey: string, status = 500) =>
  new Response(
    JSON.stringify({ detail: { code, id, message, i18n: i18nKey } }),
    { status, headers: { 'Content-Type': 'application/json' } },
  );

describe('getErrorMessage — ToukaException nested detail mapping', () => {
  it('maps SOUL_ALREADY_EXISTS (nested detail) to a human message, not raw JSON', async () => {
    const res = toukaResponse(
      'SOUL_ALREADY_EXISTS', 1401, 'Soul already exists', 'error.soul.already_exists', 409,
    );
    const msg = await getErrorMessage(res, 'CONFIG_SAVE_FAILED');
    expect(msg).not.toContain('{'); // 不得是 JSON.stringify(detail)
    expect(msg).toMatch(/靈魂已存在|Soul already exists/);
  });

  it('maps SOUL_WRITE_FAILED (nested detail) to a human message', async () => {
    const res = toukaResponse(
      'SOUL_WRITE_FAILED', 1407, 'Failed to persist soul files', 'error.soul.write_failed',
    );
    const msg = await getErrorMessage(res, 'CONFIG_SAVE_FAILED');
    expect(msg).not.toContain('{');
    expect(msg).toMatch(/靈魂檔案寫入失敗|Failed to persist soul files/);
  });

  it('falls back to nested message for a code the frontend SSOT does not know', async () => {
    const res = toukaResponse('FUTURE_CODE', 99999, 'Something specific happened', 'error.future');
    const msg = await getErrorMessage(res, 'SYSTEM_ERROR');
    expect(msg).toBe('Something specific happened'); // 優雅退路：巢內人類可讀 message，非 JSON
  });

  it('keeps supporting the legacy top-level id shape (regression)', async () => {
    const res = new Response(
      JSON.stringify({ id: 1401, msg: 'Soul already exists' }),
      { status: 409, headers: { 'Content-Type': 'application/json' } },
    );
    const msg = await getErrorMessage(res, 'CONFIG_SAVE_FAILED');
    expect(msg).toMatch(/靈魂已存在|Soul already exists/);
  });

  it('keeps returning plain string detail as-is (regression)', async () => {
    const res = new Response(JSON.stringify({ detail: 'plain failure' }), {
      status: 400, headers: { 'Content-Type': 'application/json' },
    });
    expect(await getErrorMessage(res, 'SYSTEM_ERROR')).toBe('plain failure');
  });
});

describe('Auto reviewer error codes 1118–1120 have translations (ADR-0246 U2c-6)', () => {
  it('errorMsg returns the zh-TW text, not the English fallback', async () => {
    const { errorMsg } = await import('../error_handler');
    expect(errorMsg('AUTO_REVIEW_DENIED')).toBe('Auto 審查員拒絕了這個動作');
    expect(errorMsg('AUTO_REVIEW_HEADLESS_TERMINAL')).toBe('無人在場時，Auto 模式不跑終端指令');
    expect(errorMsg('AUTO_REVIEW_UNAVAILABLE')).toBe('這個動作需要人決定，但目前沒有人在場');
  });

  it('en has the same keys', async () => {
    const en = (await import('@/locales/en/translation.json')).default;
    expect(en.error.auth.auto_review_denied).toBe('The Auto reviewer refused this action');
    expect(en.error.auth.auto_review_headless_terminal).toBe('Terminal commands do not run in Auto mode while no one is present');
    expect(en.error.auth.auto_review_unavailable).toBe('This action needs a human decision and no one is present');
  });
});

describe('getCollisionMessage — 建立時撞名要說出撞到哪個 ID', () => {
  it('AGENT_ID_COLLECTION_COLLISION 帶出 collides_with', async () => {
    const res = new Response(JSON.stringify({ detail: {
      code: 'AGENT_ID_COLLECTION_COLLISION', id: 1416, message: 'x',
      i18n: 'error.agent_id.collection_collision', collides_with: 'zz_taken',
    } }), { status: 409 });
    const msg = await getCollisionMessage(res);
    expect(msg).toContain('zz_taken');
    expect(await res.json()).toBeDefined(); // 讀 clone，不消耗原 body
  });

  it('其他錯誤碼回 undefined，交給一般訊息', async () => {
    const res = new Response(JSON.stringify({ detail: { code: 'SOUL_ALREADY_EXISTS', id: 1401 } }), { status: 409 });
    expect(await getCollisionMessage(res)).toBeUndefined();
  });
});
