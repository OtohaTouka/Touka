import { CONFIG } from './config';

/**
 * HAX G18（通知系統變更）：應用程式中介資料。
 *
 * 版本號的唯一事實來源（SSOT）是 package.json——建置時由 next.config 的 `env`
 * 只注入版本字串，前端不匯入整份 package.json、也不另存第二份，
 * 由 tests/unit/app_meta.test.ts 守衛兩者一致。
 */
export const APP_VERSION: string = CONFIG.APP_VERSION;
export const APP_NAME = 'Touka 燈火控制台';
