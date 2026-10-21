import { describe, it, expect } from 'vitest';
import { parseToolResult } from '@/lib/tool_result';

// P3：把工具結果字串解析成人面卡片可渲染的判別聯集（消費 P1/P2 結構化封套）。
describe('parseToolResult', () => {
  it('classifies permission_denied as "denied" (拒絕黃)', () => {
    const r = parseToolResult(
      '{"error_class":"permission_denied","message":"Tool blocked","suggestion":"use file verbs"}',
    );
    expect(r.kind).toBe('denied');
    expect(r.errorClass).toBe('permission_denied');
    expect(r.message).toBe('Tool blocked');
    expect(r.suggestion).toBe('use file verbs');
  });

  it('classifies other error_class as "error" (錯誤紅)', () => {
    const r = parseToolResult('{"error_class":"validation_error","message":"bad args"}');
    expect(r.kind).toBe('error');
    expect(r.message).toBe('bad args');
  });

  it('classifies structured success (ok=true) with preview', () => {
    const r = parseToolResult('{"ok":true,"message":"Edited foo.py","preview":"> 10| new line"}');
    expect(r.kind).toBe('success');
    expect(r.message).toBe('Edited foo.py');
    expect(r.preview).toBe('> 10| new line');
  });

  it('tolerates a trailing footer after the JSON object', () => {
    const r = parseToolResult(
      '{"error_class":"resource_not_found","message":"nope"}\n[系統自動附加] 時間…',
    );
    expect(r.kind).toBe('error');
    expect(r.message).toBe('nope');
  });

  it('falls back to raw for plain strings (bash markdown / legacy)', () => {
    const r = parseToolResult('<details><summary>bash</summary>output</details>');
    expect(r.kind).toBe('raw');
    expect(r.raw).toContain('bash');
  });

  it('falls back to raw for status payloads (running/approval_pending)', () => {
    const r = parseToolResult('{"status":"running","execution_id":"e1"}');
    expect(r.kind).toBe('raw');
  });

  it('handles null/object results without throwing', () => {
    expect(parseToolResult(null).kind).toBe('raw');
    expect(parseToolResult({ any: 'obj' }).kind).toBe('raw');
  });
});
