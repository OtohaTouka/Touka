import { describe, it, expect } from 'vitest';
import { Project } from 'ts-morph';
import { checkXAgentIdViolations, DEFAULT_XAGENTID_GUARD_CONFIG } from '../xagentid_guard_logic';

describe('X-Agent-ID Identity Guard Logic', () => {
  const project = new Project({ useInMemoryFileSystem: true });

  it('should detect X-Agent-ID in PropertyAssignment key', () => {
    const code = `
      const options = {
        headers: {
          "X-Agent-ID": "DebugAgent"
        }
      };
    `;
    const sf = project.createSourceFile('src/lib/connections.ts', code, { overwrite: true });
    const violations = checkXAgentIdViolations(sf, DEFAULT_XAGENTID_GUARD_CONFIG);
    expect(violations.length).toBe(1);
    expect(violations[0].message).toContain('X-Agent-ID');
  });

  it('should detect X-Agent-ID key with lowercase or other case', () => {
    const code = `
      const options = {
        headers: {
          "x-agent-id": "DebugAgent"
        }
      };
    `;
    const sf = project.createSourceFile('src/lib/connections.ts', code, { overwrite: true });
    const violations = checkXAgentIdViolations(sf, DEFAULT_XAGENTID_GUARD_CONFIG);
    expect(violations.length).toBe(1);
  });

  it('should detect X-Agent-ID key as a non-string identifier', () => {
    // 雖然 header key 一般需要引號，但 JS 物件語法也允許無引號（若符合變數命名規範），或帶單引號
    const code = `
      const options = {
        headers: {
          'X-Agent-ID': 'DebugAgent'
        }
      };
    `;
    const sf = project.createSourceFile('src/lib/connections.ts', code, { overwrite: true });
    const violations = checkXAgentIdViolations(sf, DEFAULT_XAGENTID_GUARD_CONFIG);
    expect(violations.length).toBe(1);
  });

  it('should detect X-Agent-ID key in ElementAccessExpression', () => {
    const code = `
      const headers = {};
      headers["X-Agent-ID"] = "DebugAgent";
    `;
    const sf = project.createSourceFile('src/lib/connections.ts', code, { overwrite: true });
    const violations = checkXAgentIdViolations(sf, DEFAULT_XAGENTID_GUARD_CONFIG);
    expect(violations.length).toBe(1);
  });

  it('should NOT scan test files', () => {
    const code = `
      const options = {
        headers: {
          "X-Agent-ID": "DebugAgent"
        }
      };
    `;
    const sf = project.createSourceFile('src/lib/connections.test.ts', code, { overwrite: true });
    const violations = checkXAgentIdViolations(sf, DEFAULT_XAGENTID_GUARD_CONFIG);
    expect(violations.length).toBe(0);
  });

  it('should respect // noqa: IDENTITY_SSOT suppression', () => {
    const code = `
      const options = {
        headers: {
          "X-Agent-ID": "DebugAgent" // noqa: IDENTITY_SSOT
        }
      };
    `;
    const sf = project.createSourceFile('src/lib/connections.ts', code, { overwrite: true });
    const violations = checkXAgentIdViolations(sf, DEFAULT_XAGENTID_GUARD_CONFIG);
    expect(violations.length).toBe(0);
  });
});
