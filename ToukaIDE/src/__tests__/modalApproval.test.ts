import { describe, expect, it, vi } from 'vitest';

import { AuthLevel } from '../mcp/authPolicy';
import { requestIdeApproval } from '../mcp/modalApproval';

describe('ToukaIDE VS Code modal HITL', () => {
    it('shows a modal for every Manual request without a human approval', async () => {
        const present = vi.fn(async () => '允許');

        await expect(requestIdeApproval(
            AuthLevel.Manual,
            false,
            '執行終端機指令',
            'npm test',
            present,
        )).resolves.toBe(true);
        expect(present).toHaveBeenCalledWith(
            'Touka 請求授權: 執行終端機指令',
            { modal: true, detail: 'npm test' },
            '允許',
            '拒絕',
        );
    });

    it('refuses when the human rejects or dismisses the modal', async () => {
        for (const choice of ['拒絕', undefined]) {
            const present = vi.fn(async () => choice);
            await expect(requestIdeApproval(
                AuthLevel.Manual,
                false,
                'Git Commit',
                'msg',
                present,
            )).resolves.toBe(false);
            expect(present).toHaveBeenCalledTimes(1);
        }
    });

    it('does not ask again after a verified human approval on the Touka card', async () => {
        const present = vi.fn(async () => '拒絕');

        await expect(requestIdeApproval(
            AuthLevel.Manual,
            true,
            '執行終端機指令',
            'npm test',
            present,
        )).resolves.toBe(true);
        expect(present).not.toHaveBeenCalled();
    });

    it('does not open a modal in YOLO', async () => {
        const present = vi.fn(async () => '拒絕');

        await expect(requestIdeApproval(
            AuthLevel.YOLO,
            false,
            'Git Commit',
            'msg',
            present,
        )).resolves.toBe(true);
        expect(present).not.toHaveBeenCalled();
    });
});
