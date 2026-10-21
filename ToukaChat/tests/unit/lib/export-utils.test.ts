import { describe, it, expect } from 'vitest';
import { formatToOpenWebUI, splitSessionMessages } from '@/lib/export-utils';
import { Session } from '@/lib/types';
import { TreeHistory } from '@/lib/tree_history';

describe('export-utils formatToOpenWebUI', () => {
    it('should format session with flat array messages correctly', () => {
        const session: Session = {
            id: 'test-id-1',
            title: 'Test Session',
            updatedAt: 1000000,
            messages: [
                {
                    id: 'msg-1',
                    parentId: null,
                    childrenIds: ['msg-2'],
                    role: 'user',
                    content: 'Hello',
                    createdAt: 2000000
                },
                {
                    id: 'msg-2',
                    parentId: 'msg-1',
                    childrenIds: [],
                    role: 'assistant',
                    content: 'Hi',
                    createdAt: 3000000
                }
            ]
        };

        const result = formatToOpenWebUI(session, 'gpt-4o');
        expect(result.id).toBe('test-id-1');
        expect(result.title).toBe('Test Session');
        expect(result.chat.models).toContain('gpt-4o');
        expect(Object.keys(result.chat.history.messages).length).toBe(2);
        expect(result.chat.history.messages['msg-1'].content).toBe('Hello');
        expect(result.created_at).toBe(Math.floor(2000000 / 1000));
    });

    it('should format session with TreeHistory messages correctly', () => {
        const treeHistory: TreeHistory = {
            currentId: 'msg-2',
            messages: {
                'msg-1': {
                    id: 'msg-1',
                    parentId: null,
                    childrenIds: ['msg-2'],
                    role: 'user',
                    content: 'Hello Tree',
                    createdAt: 4000000
                },
                'msg-2': {
                    id: 'msg-2',
                    parentId: 'msg-1',
                    childrenIds: [],
                    role: 'assistant',
                    content: 'Hi Tree',
                    createdAt: 5000000
                }
            }
        };

        const session: Session = {
            id: 'test-id-2',
            title: 'Tree Session',
            updatedAt: 1000000,
            messages: treeHistory
        };

        const result = formatToOpenWebUI(session, 'claude-3');
        expect(result.id).toBe('test-id-2');
        expect(result.title).toBe('Tree Session');
        expect(result.chat.models).toContain('claude-3');
        expect(Object.keys(result.chat.history.messages).length).toBe(2);
        expect(result.chat.history.messages['msg-2'].content).toBe('Hi Tree');
        expect(result.chat.history.messages['msg-1'].timestamp).toBe(Math.floor(4000000 / 1000));
    });

    it('should use session.agent_id or home_for_agent over selectedModel', () => {
        const session: Session = {
            id: 'test-id-3',
            title: 'Agent Session',
            updatedAt: 1000000,
            agent_id: 'Bob',
            messages: []
        };

        const result = formatToOpenWebUI(session, 'GirlG');
        expect(result.chat.models).toContain('Bob');
        
        const session2: Session = {
            id: 'test-id-4',
            title: 'Home Session',
            updatedAt: 1000000,
            home_for_agent: 'Alice',
            messages: []
        };

        const result2 = formatToOpenWebUI(session2, 'GirlG');
        expect(result2.chat.models).toContain('Alice');
    });
});

describe('export-utils splitSessionMessages', () => {
    it('should return null if messages length is less than or equal to keepCount', () => {
        const session: Session = {
            id: 'test-id-1',
            title: 'Test Session',
            updatedAt: 1000000,
            messages: [
                { id: '1', role: 'user', content: 'A', childrenIds: ['2'], parentId: null },
                { id: '2', role: 'assistant', content: 'B', childrenIds: [], parentId: '1' }
            ]
        };
        const result = splitSessionMessages(session, 5);
        expect(result).toBeNull();
    });

    it('should split linear messages correctly when rounds count is greater than keepCount', () => {
        // Session has 4 messages = 2 rounds. keepCount=1 (round unit) → split after round 1.
        // Legacy: [msg1, msg2] (round 1); Keep: [msg3, msg4] (round 2). (ADR-0036)
        const session: Session = {
            id: 'test-id-2',
            title: 'Test Session',
            updatedAt: 1000000,
            messages: [
                { id: '1', role: 'user', content: 'A', childrenIds: ['2'], parentId: null },
                { id: '2', role: 'assistant', content: 'B', childrenIds: ['3'], parentId: '1' },
                { id: '3', role: 'user', content: 'C', childrenIds: ['4'], parentId: '2' },
                { id: '4', role: 'assistant', content: 'D', childrenIds: [], parentId: '3' }
            ]
        };
        const result = splitSessionMessages(session, 1); // keepCount = 1 round
        expect(result).not.toBeNull();
        expect(result?.legacyCount).toBe(2);
        expect(result?.legacyMessages.length).toBe(2);
        expect(result?.legacyMessages[0].id).toBe('1');
        expect(result?.legacyMessages[1].id).toBe('2');
        expect(result?.keepMessages.length).toBe(2);
        expect(result?.keepMessages[0].id).toBe('3');
        expect(result?.keepMessages[1].id).toBe('4');
    });
});







