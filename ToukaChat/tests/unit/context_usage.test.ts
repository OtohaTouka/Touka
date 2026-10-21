import { describe, it, expect } from 'vitest';
import { countRounds, contextUsagePct, CONTEXT_ROUND_LIMIT } from '@/lib/context_usage';
import { MessageNode } from '@/lib/tree_history';

const mk = (id: string, role: MessageNode['role']): MessageNode => ({
  id, role, content: '', parentId: null, childrenIds: [],
});

describe('countRounds', () => {
  it('counts user→assistant pairs', () => {
    expect(
      countRounds([mk('1', 'user'), mk('2', 'assistant'), mk('3', 'user'), mk('4', 'assistant')]),
    ).toBe(2);
  });

  it('ignores a trailing user with no assistant reply', () => {
    expect(countRounds([mk('1', 'user'), mk('2', 'assistant'), mk('3', 'user')])).toBe(1);
  });

  it('skips system messages between user and assistant', () => {
    expect(countRounds([mk('1', 'user'), mk('2', 'system'), mk('3', 'assistant')])).toBe(1);
  });

  it('returns 0 for empty', () => {
    expect(countRounds([])).toBe(0);
  });
});

describe('contextUsagePct', () => {
  it('computes percentage vs limit', () => {
    expect(contextUsagePct(75, 150)).toBe(50);
    expect(contextUsagePct(150, 150)).toBe(100);
  });

  it('caps at 100', () => {
    expect(contextUsagePct(300, 150)).toBe(100);
  });

  it('uses the default round limit', () => {
    expect(contextUsagePct(CONTEXT_ROUND_LIMIT)).toBe(100);
  });
});
