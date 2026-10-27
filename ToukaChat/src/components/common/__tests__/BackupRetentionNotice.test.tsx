import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import zhTW from '@/locales/zh-TW/translation.json';
import en from '@/locales/en/translation.json';
import { BackupRetentionNotice } from '../BackupRetentionNotice';

// ADR-0270 補記（hist-bak a）：刪除與重置畫面的「還有備份」提醒。位置是固定文字，伺服器不回傳路徑。
const KEYS = [
    'summary', 'still_there', 'still_there_delete', 'still_there_scrubbed', 'still_there_scrubbed_partial',
    'where_snapshots', 'where_upgrade', 'purge_title',
    'purge_scrub', 'purge_scrub_delete', 'purge_snapshot', 'purge_upgrade', 'no_auto_snapshot',
    'restore_effect', 'not_context_snapshot',
] as const;

describe('BackupRetentionNotice', () => {
    it.each([['zh-TW', zhTW], ['en', en]] as const)('B01 %s 每句提醒都有翻譯，位置寫對、不寫過時或無關的位置', (_lng, catalog) => {
        const notice = (catalog as unknown as { backup_notice?: Record<string, string> }).backup_notice ?? {};
        for (const key of KEYS) expect(notice[key], key).toMatch(/\S/);
        expect(notice.where_snapshots).toMatch(/\.env/);
        expect(notice.where_snapshots).toMatch(/backups/);
        expect(notice.where_snapshots).toMatch(/Touka_Backup_/);
        expect(notice.where_snapshots).toMatch(/Touka_AutoBackup_/);
        expect(notice.where_upgrade).toMatch(/vault\/core_memory_backups/);
        for (const key of ['still_there', 'still_there_delete', 'still_there_scrubbed', 'still_there_scrubbed_partial', 'restore_effect']) expect(notice[key]).toContain('{{id}}');
        const all = Object.values(notice).join('\n');
        expect(all).not.toMatch(/WORKSPACE_ROOT|執行器|executor|家目錄|home dir/i);
    });

    it('B02 重置：列出勾選、刪整份快照、升級備份手動刪，還原副作用與情境快照的區分', () => {
        render(<BackupRetentionNotice variant="reset" agentId="Jin" />);
        expect(screen.getByText(/「Jin」的資料還留在系統快照裡/)).toBeDefined();
        expect(screen.getByText(/備份在哪裡、怎麼徹底刪/)).toBeDefined();
        expect(screen.getByText(/建議：重置時勾選/)).toBeDefined();
        expect(screen.getByText(/刪整份快照.*其他靈魂/)).toBeDefined();
        expect(screen.getByText(/所有靈魂共用.*手動刪/)).toBeDefined();
        expect(screen.getByText(/「Jin」的舊資料整個帶回來.*所有靈魂的記憶都退回/)).toBeDefined();
        expect(screen.getByText(/情境快照.*兩回事/)).toBeDefined();
        expect(screen.queryByText(/刪除前不會自動存快照/)).toBeNull();
    });

    it('B03 刪除：寫「可能還留著」、刪除前先重置並勾選，以及刪除前不會自動存快照', () => {
        render(<BackupRetentionNotice variant="delete" agentId="MySoul" />);
        expect(screen.getByText(/「MySoul」的資料可能還留在之前的系統快照裡/)).toBeDefined();
        expect(screen.getByText(/建議：刪除前先到「代理人資料重置」重置/)).toBeDefined();
        expect(screen.getByText(/刪整份快照/)).toBeDefined();
        expect(screen.getByText(/刪除前不會自動存快照/)).toBeDefined();
        expect(screen.getByText(/所有靈魂的記憶都退回/)).toBeDefined();
        expect(screen.queryByText(/建議：重置時勾選/)).toBeNull();
    });

    it('B05b 清除完成、但還有不在範圍的舊資料：照實說留在備份裡，列快照與升級備份，不叫人再勾選（丙-3）', () => {
        render(<BackupRetentionNotice variant="reset_scrubbed_partial" agentId="Jin" />);
        expect(screen.getByText(/範圍內的記憶已從備份清掉，但還有舊格式或舊前綴的記憶不在清除範圍/)).toBeDefined();
        expect(screen.getByText(/「Jin」的對話與其他資料也還留在系統快照裡/)).toBeDefined();
        expect(screen.queryByText(/^記憶已從備份清掉/)).toBeNull();
        expect(screen.getByText(/vault\/core_memory_backups/)).toBeDefined();
        expect(screen.getByText(/刪整份快照/)).toBeDefined();
        expect(screen.queryByText(/建議：重置時勾選/)).toBeNull();
    });

    it('B05 清除連備份完成後：只講快照裡的對話與其他資料、快照位置與刪整份快照，不提升級備份與勾選', () => {
        render(<BackupRetentionNotice variant="reset_scrubbed" agentId="Jin" />);
        expect(screen.getByText(/記憶已從備份清掉，但「Jin」的對話與其他資料還留在系統快照裡/)).toBeDefined();
        expect(screen.getByText(/Touka_AutoBackup_/)).toBeDefined();
        expect(screen.getByText(/刪整份快照/)).toBeDefined();
        expect(screen.getByText(/所有靈魂的記憶都退回/)).toBeDefined();
        expect(screen.queryByText(/vault\/core_memory_backups/)).toBeNull();
        expect(screen.queryByText(/升級備份/)).toBeNull();
        expect(screen.queryByText(/勾選/)).toBeNull();
    });

    it('B04 DebugAgent：只寫刪整份快照，不提勾選', () => {
        render(<BackupRetentionNotice variant="debug" agentId="DebugAgent" />);
        expect(screen.getByText(/「DebugAgent」的資料還留在系統快照裡/)).toBeDefined();
        expect(screen.getByText(/刪整份快照/)).toBeDefined();
        expect(screen.getByText(/所有靈魂共用.*手動刪/)).toBeDefined();
        expect(screen.getByText(/情境快照.*兩回事/)).toBeDefined();
        expect(screen.queryByText(/勾選/)).toBeNull();
        expect(screen.queryByText(/刪除前不會自動存快照/)).toBeNull();
    });
});
