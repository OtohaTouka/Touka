import pkg from '../../package.json';

/**
 * HAX G18（通知系統變更）：應用程式中介資料。
 *
 * 版本號的唯一事實來源（SSOT）是 package.json——前端只讀、不另存第二份，
 * 由 tests/unit/app_meta.test.ts 守衛兩者一致。
 */
export const APP_VERSION: string = pkg.version;
export const APP_NAME = 'Touka 燈火控制台';
