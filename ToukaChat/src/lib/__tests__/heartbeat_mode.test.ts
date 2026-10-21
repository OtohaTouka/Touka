import { describe, it, expect } from 'vitest';
import { heartbeatMode } from '../heartbeat_mode';

describe('heartbeatMode（ADR-0246：和後端心跳對應一致）', () => {
  it('safe→Auto、full→YOLO', () => {
    expect(heartbeatMode('safe')).toBe('Auto');
    expect(heartbeatMode('full')).toBe('YOLO');
  });

  it.each([undefined, null, '', 'readonly', 'Safe', 'constructor', 'toString'])('看不懂的值 %s 一律 Manual', value => {
    expect(heartbeatMode(value)).toBe('Manual');
  });
});
