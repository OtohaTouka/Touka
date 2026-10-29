/**
 * hitl-unification H3 — 審批事件 → 樹（applyEventToTree）純函式測試。
 *
 * tool_approval_request（H2 統一 broker）以 call_id 建 awaiting_approval 步驟、
 * 事件 id＝exec_id 掛進 approval；核准後 tool_start 就地升級 running（不重複建行）；
 * tool_approval_resolved 記 decision；拒絕/逾時由後續 tool_result(error) 收斂行狀態
 * （P3 permission_denied → denied 黃）。多筆並行各自獨立（grok parallel calls）。
 */
import { describe, it, expect } from 'vitest';
import { applyEventToTree, appendAssistantNode } from '../stream_tree';
import { TreeHistory } from '../tree_history';

function baseTreeWithAssistant(): { tree: TreeHistory; aid: string } {
  const { tree, assistantMsgId } = appendAssistantNode({ messages: {}, currentId: null });
  return { tree, aid: assistantMsgId };
}

const approvalEvent = (execId: string, callId: string, tool = 'write') => ({
  type: 'tool_approval_request',
  id: execId,
  call_id: callId,
  tool,
  args: { scope: 'vault', path: 'a.md' },
  agent_id: 'Sumi',
  risk: 'safe',
  timeout: 180,
});

describe('stream_tree — tool approval events (H3)', () => {
  it('tool_approval_request creates an awaiting step keyed by call_id with exec meta', () => {
    const { tree, aid } = baseTreeWithAssistant();
    const next = applyEventToTree(tree, aid, approvalEvent('ex1', 'c1'));
    const steps = next.messages[aid].steps!;
    expect(steps).toHaveLength(1);
    expect(steps[0]).toMatchObject({
      id: 'c1',
      tool: 'write',
      status: 'awaiting_approval',
      approval: { execId: 'ex1', risk: 'safe', timeout: 180 },
    });
  });

  it('approved flow: resolved sets decision, tool_start upgrades in place (no duplicate row)', () => {
    const { tree, aid } = baseTreeWithAssistant();
    let t = applyEventToTree(tree, aid, approvalEvent('ex1', 'c1'));
    t = applyEventToTree(t, aid, {
      type: 'tool_approval_resolved', id: 'ex1', call_id: 'c1', tool: 'write', decision: 'approved',
    });
    t = applyEventToTree(t, aid, { type: 'tool_start', id: 'c1', tool: 'write', args: { scope: 'vault' } });

    const steps = t.messages[aid].steps!;
    expect(steps).toHaveLength(1); // 就地升級，不重複建行
    expect(steps[0].status).toBe('running');
    expect(steps[0].approval?.decision).toBe('approved');

    const done = applyEventToTree(t, aid, { type: 'tool_result', id: 'c1', status: 'success', result: '{"ok":true}' });
    expect(done.messages[aid].steps![0].status).toBe('success');
  });

  it('rejected flow: decision recorded, tool_result(error) closes the row', () => {
    const { tree, aid } = baseTreeWithAssistant();
    let t = applyEventToTree(tree, aid, approvalEvent('ex2', 'c2'));
    t = applyEventToTree(t, aid, {
      type: 'tool_approval_resolved', id: 'ex2', call_id: 'c2', tool: 'write', decision: 'rejected',
    });
    t = applyEventToTree(t, aid, {
      type: 'tool_result', id: 'c2', status: 'error',
      result: '{"error_class":"permission_denied","message":"rejected","reason":"approval_rejected"}',
    });
    const step = t.messages[aid].steps![0];
    expect(step.status).toBe('error');
    expect(step.approval?.decision).toBe('rejected');
  });

  it('timeout decision recorded (backend auto-reject)', () => {
    const { tree, aid } = baseTreeWithAssistant();
    let t = applyEventToTree(tree, aid, approvalEvent('ex3', 'c3'));
    t = applyEventToTree(t, aid, {
      type: 'tool_approval_resolved', id: 'ex3', call_id: 'c3', tool: 'write', decision: 'timeout',
    });
    expect(t.messages[aid].steps![0].approval?.decision).toBe('timeout');
  });

  it('parallel approvals stay independent (grok parallel tool calls)', () => {
    const { tree, aid } = baseTreeWithAssistant();
    let t = applyEventToTree(tree, aid, approvalEvent('exA', 'cA', 'write'));
    t = applyEventToTree(t, aid, approvalEvent('exB', 'cB', 'send_message'));
    t = applyEventToTree(t, aid, {
      type: 'tool_approval_resolved', id: 'exB', call_id: 'cB', tool: 'send_message', decision: 'rejected',
    });

    const steps = t.messages[aid].steps!;
    expect(steps).toHaveLength(2);
    expect(steps.find(s => s.id === 'cA')!.approval?.decision).toBeUndefined(); // A 仍待決
    expect(steps.find(s => s.id === 'cB')!.approval?.decision).toBe('rejected');
  });

  it('plain tool_start without prior approval still appends (existing behavior preserved)', () => {
    const { tree, aid } = baseTreeWithAssistant();
    const t = applyEventToTree(tree, aid, { type: 'tool_start', id: 'p1', tool: 'read', args: {} });
    expect(t.messages[aid].steps![0]).toMatchObject({ id: 'p1', status: 'running' });
  });

  it('MRTR interaction keeps only local id/schema and resolves independently', () => {
    const { tree, aid } = baseTreeWithAssistant();
    let t = applyEventToTree(tree, aid, {
      type: 'mcp_interaction_request',
      id: 'mcpint_local',
      call_id: 'call-1',
      tool: 'srv__demo',
      kind: 'elicitation_form',
      message: 'Choose',
      schema: {
        type: 'object',
        properties: { answer: { type: 'string' } },
      },
      timeout: 120,
    });
    expect(t.messages[aid].steps![0]).toMatchObject({
      id: 'mcpint_local',
      status: 'awaiting_approval',
      interaction: {
        execId: 'mcpint_local',
        kind: 'elicitation_form',
        message: 'Choose',
      },
    });
    expect(JSON.stringify(t)).not.toContain('requestState');

    t = applyEventToTree(t, aid, {
      type: 'mcp_interaction_resolved',
      id: 'mcpint_local',
      call_id: 'call-1',
      tool: 'srv__demo',
      decision: 'accepted',
    });
    expect(t.messages[aid].steps![0].interaction?.decision).toBe('accepted');
    expect(t.messages[aid].steps![0].status).toBe('success');
  });
});

describe('stream_tree — Auto reviewer fields (ADR-0246 U2c-6)', () => {
  it('keeps review_reason and human_only on the step, also after the card resolves', () => {
    const { tree, aid } = baseTreeWithAssistant();
    let t = applyEventToTree(tree, aid, {
      ...approvalEvent('ex9', 'c9', 'execute_command'),
      review_reason: 'The request does not mention deleting files.',
      human_only: true,
    });
    expect(t.messages[aid].steps![0].approval).toEqual({
      execId: 'ex9', risk: 'safe', timeout: 180,
      reviewReason: 'The request does not mention deleting files.', humanOnly: true,
    });
    t = applyEventToTree(t, aid, {
      type: 'tool_approval_resolved', id: 'ex9', call_id: 'c9', tool: 'execute_command', decision: 'approved',
    });
    expect(t.messages[aid].steps![0].approval).toEqual({
      execId: 'ex9', risk: 'safe', timeout: 180,
      reviewReason: 'The request does not mention deleting files.', humanOnly: true, decision: 'approved',
    });
  });

  it('keeps hook_reason (ADR-0292), including an empty one, apart from review_reason', () => {
    const { tree, aid } = baseTreeWithAssistant();
    let t = applyEventToTree(tree, aid, { ...approvalEvent('ex7', 'c7'), hook_reason: 'Touches the release folder.' });
    expect(t.messages[aid].steps![0].approval).toEqual({
      execId: 'ex7', risk: 'safe', timeout: 180, hookReason: 'Touches the release folder.',
    });
    t = applyEventToTree(tree, aid, { ...approvalEvent('ex6', 'c6'), hook_reason: '' });
    expect(t.messages[aid].steps![0].approval).toEqual({ execId: 'ex6', risk: 'safe', timeout: 180, hookReason: '' });
  });

  it('ignores the fields when absent or of the wrong type', () => {
    const { tree, aid } = baseTreeWithAssistant();
    const t = applyEventToTree(tree, aid, { ...approvalEvent('ex8', 'c8'), review_reason: 7, human_only: 'yes', hook_reason: 3 });
    expect(t.messages[aid].steps![0].approval).toEqual({ execId: 'ex8', risk: 'safe', timeout: 180 });
  });
});

describe('stream_tree — isolation on the approval card (iso-c2)', () => {
  const isolationOf = (isolation: unknown) => {
    const { tree, aid } = baseTreeWithAssistant();
    const t = applyEventToTree(tree, aid, { ...approvalEvent('ex7', 'c7', 'execute_command'), isolation });
    return t.messages[aid].steps![0].approval?.isolation;
  };

  it('keeps the isolation payload, also after the card resolves', () => {
    const { tree, aid } = baseTreeWithAssistant();
    let t = applyEventToTree(tree, aid, {
      ...approvalEvent('ex6', 'c6', 'execute_command'),
      isolation: {
        level: 'none', fallback: 'direct', features: [],
        reason: 'sandbox_state_unconfigured', reason_detail: 'not_writable',
      },
    });
    const expected = {
      level: 'none', fallback: 'direct', features: [],
      reason: 'sandbox_state_unconfigured', reasonDetail: 'not_writable',
    };
    expect(t.messages[aid].steps![0].approval?.isolation).toEqual(expected);
    t = applyEventToTree(t, aid, {
      type: 'tool_approval_resolved', id: 'ex6', call_id: 'c6', tool: 'execute_command', decision: 'approved',
    });
    expect(t.messages[aid].steps![0].approval?.isolation).toEqual(expected);
  });

  it('reads features from a name list or a map of flags', () => {
    expect(isolationOf({ level: 'none', fallback: 'landlock', features: ['fs', 'tcp', 7] })?.features)
      .toEqual(['fs', 'tcp']);
    expect(isolationOf({ level: 'none', fallback: 'landlock', features: { fs: true, tcp: true, signal: false } })?.features)
      .toEqual(['fs', 'tcp']);
    expect(isolationOf({ level: 'none', fallback: 'landlock' })?.features).toEqual([]);
  });

  it('leaves the card as before when the field is absent or null', () => {
    expect(isolationOf(undefined)).toBeUndefined();
    expect(isolationOf(null)).toBeUndefined();
  });

  it('turns an unreadable payload into none without a fallback (the card then blocks approval)', () => {
    const blocked = { level: 'none', fallback: null, features: [] };
    expect(isolationOf('direct')).toEqual(blocked);
    expect(isolationOf({ level: 'none' })).toEqual(blocked);
    expect(isolationOf({ level: 'none', fallback: 'container' })).toEqual(blocked);
    expect(isolationOf({ level: 'sandboxed', fallback: null })).toEqual(blocked);
  });

  // C2-2：降級去處可以是執行器；卡片文案要的事實（部署預期、網路、封鎖埠、本機原因）照收
  it('keeps the executor fallback and the facts the wording needs', () => {
    expect(isolationOf({
      level: 'none', fallback: 'executor', features: ['fs'], reason: 'project_not_in_executor',
      expected: 'executor', net: 'net_mptcp', block_ports_issue: 'block_ports_incomplete',
      local_issue: 'probe_pending',
    })).toEqual({
      level: 'none', fallback: 'executor', features: ['fs'], reason: 'project_not_in_executor',
      expected: 'executor', net: 'net_mptcp', blockPortsIssue: 'block_ports_incomplete',
      localIssue: 'probe_pending',
    });
  });

  it('drops facts of the wrong type and an unknown expected level', () => {
    expect(isolationOf({
      level: 'none', fallback: 'landlock', features: [], expected: 'sandboxed', net: 7,
      block_ports_issue: null, local_issue: ['x'],
    })).toEqual({ level: 'none', fallback: 'landlock', features: [] });
  });

  it('drops the fallback when the command runs isolated', () => {
    expect(isolationOf({ level: 'landlock', fallback: 'direct', features: ['fs'] }))
      .toEqual({ level: 'landlock', fallback: null, features: ['fs'] });
  });

  // C2-3 必做 ③：未隔離卡沒有完整顯示要執行的內容時，後端只送 complete: false
  it('keeps complete: false and treats any value but true as incomplete', () => {
    const direct = { level: 'none', fallback: 'direct', features: [] };
    expect(isolationOf({ ...direct, complete: false })).toEqual({ ...direct, complete: false });
    expect(isolationOf({ ...direct, complete: 'no' })).toEqual({ ...direct, complete: false });
    expect(isolationOf({ ...direct, complete: null })).toEqual({ ...direct, complete: false });
    expect(isolationOf({ ...direct, complete: true })).toEqual(direct);
    expect(isolationOf(direct)).toEqual(direct);
  });
});
