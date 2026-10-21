import { describe, it, expect } from 'vitest';
import { Project } from 'ts-morph';
import { checkEventSourceViolations, DEFAULT_EVENTSOURCE_GUARD_CONFIG } from '../eventsource_guard_logic';

describe('EventSource Token Guard Logic', () => {
  const project = new Project({ useInMemoryFileSystem: true });

  it('should flag EventSource instantiation regardless of url parameters', () => {
    const sf = project.createSourceFile('src/hooks/test.ts', `
      const url = "/stream?token=abc";
      const es = new EventSource(url);
    `, { overwrite: true });
    const violations = checkEventSourceViolations(sf, DEFAULT_EVENTSOURCE_GUARD_CONFIG);
    expect(violations).toHaveLength(1);
    expect(violations[0].message).toContain('禁止直接使用原生 EventSource');
  });

  it('should flag EventSource instantiation with hardcoded string URL', () => {
    const sf = project.createSourceFile('src/hooks/test.ts', `
      const es = new EventSource("/stream?sessionId=123&token=abc");
    `, { overwrite: true });
    const violations = checkEventSourceViolations(sf, DEFAULT_EVENTSOURCE_GUARD_CONFIG);
    expect(violations).toHaveLength(1);
  });

  it('should respect // noqa: EVENTSOURCE_SSOT suppression', () => {
    const sf = project.createSourceFile('src/hooks/test.ts', `
      const url = "/stream";
      const es = new EventSource(url); // noqa: EVENTSOURCE_SSOT
    `, { overwrite: true });
    const violations = checkEventSourceViolations(sf, DEFAULT_EVENTSOURCE_GUARD_CONFIG);
    expect(violations).toHaveLength(0);
  });
});
