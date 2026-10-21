import { describe, it, expect, beforeEach } from 'vitest';

// 模擬後端 OpenApiParser
class MockBackendParser {
  getRoutes() {
    return [
      '/v1/agents',
      '/v1/agents/{agent_id}',
      '/v1/heartbeat/awaken',
      '/v1/debug/execute'
    ];
  }
}

// 核心比對引擎
class ParityEngine {
  private routes: Set<string>;
  private regexRoutes: { pattern: RegExp; original: string }[];

  constructor(routes: string[]) {
    this.routes = new Set(routes);
    this.regexRoutes = routes.map(r => ({
      pattern: new RegExp('^' + r.replace(/{[^}]+}/g, '[^/]+') + '$'),
      original: r
    }));
  }

  validate(url: string): { valid: boolean; match?: string } {
    // 移除 Query String
    const purePath = url.split('?')[0];
    
    if (this.routes.has(purePath)) {
      return { valid: true, match: purePath };
    }

    const match = this.regexRoutes.find(r => r.pattern.test(purePath));
    if (match) {
      return { valid: true, match: match.original };
    }

    return { valid: false };
  }
}

describe('API Parity Engine', () => {
  let engine: ParityEngine;

  beforeEach(() => {
    const parser = new MockBackendParser();
    engine = new ParityEngine(parser.getRoutes());
  });

  it('應正確匹配簡單路徑', () => {
    expect(engine.validate('/v1/agents').valid).toBe(true);
    expect(engine.validate('/v1/heartbeat/awaken').valid).toBe(true);
  });

  it('應正確匹配動態路徑', () => {
    expect(engine.validate('/v1/agents/Alice').valid).toBe(true);
    expect(engine.validate('/v1/agents/123').valid).toBe(true);
  });

  it('應拒絕不存在的路徑', () => {
    expect(engine.validate('/v1/not-exist').valid).toBe(false);
    expect(engine.validate('/v1/agent').valid).toBe(false); // 拼錯
  });

  it('應忽略 Query String 進行匹配', () => {
    expect(engine.validate('/v1/agents?limit=10').valid).toBe(true);
  });
});
