import { useState, useCallback } from 'react';
import { logSSOTError } from '../lib/error_handler';

/** Exact-key deletion reports failure without exposing stored data or exceptions. */
export function removeLocalStorageItem(key: string): boolean {
  if (typeof window === 'undefined') return true;
  try {
    window.localStorage.removeItem(key);
    return true;
  } catch {
    return false;
  }
}

/** Exact-key write reports failure without exposing the value or the exception. */
export function replaceLocalStorageItem<T>(key: string, value: T): boolean {
  if (typeof window === 'undefined') return true;
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
    return true;
  } catch {
    return false;
  }
}

/**
 * 規格化讀取 LocalStorage（相容舊版純字串、布林值與標準 JSON 格式）
 */
export function getLocalStorageItem<T>(key: string, fallback: T): T {
  if (typeof window === 'undefined') return fallback;
  try {
    const item = window.localStorage.getItem(key);
    if (!item) return fallback;
    try {
      return JSON.parse(item);
    } catch {
      // 容錯機制：當原本儲存的不是 JSON 格式時，針對 string 和 boolean 進行相容處理
      if (typeof fallback === 'string') {
        return item as unknown as T;
      }
      if (typeof fallback === 'boolean') {
        return (item === 'true') as unknown as T;
      }
      return fallback;
    }
  } catch (error) {
    logSSOTError(error, `getLocalStorageItem:${key}`);
    return fallback;
  }
}

/**
 * 規格化寫入 LocalStorage（localStorage SSOT 授權通道；禁令 9）
 */
export function setLocalStorageItem<T>(key: string, value: T): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch (error) {
    logSSOTError(error, `setLocalStorageItem:${key}`);
  }
}

/**
 * 統一的 LocalStorage 存取 Hook。
 * 封裝原始 localStorage 調用，提供 React 狀態同步。
 *
 * `sanitize`：進出瀏覽器儲存的值都先經過它（初始讀取、每次寫入）；記憶體裡的狀態保留呼叫端設的值。
 * 用來讓某些欄位只活在記憶體、不落到瀏覽器（例如連線金鑰換成遮罩）。請傳模組層級的穩定函式。
 */
export function useLocalStorage<T>(key: string, initialValue: T, sanitize?: (value: T) => T) {
  // 獲取初始值
  const [storedValue, setStoredValue] = useState<T>(() => {
    const value = getLocalStorageItem<T>(key, initialValue);
    return sanitize ? sanitize(value) : value;
  });

  // 當 storedValue 改變時更新 localStorage
  const setValue = useCallback((value: T | ((val: T) => T)) => {
    try {
      setStoredValue((currentVal) => {
        const valueToStore = value instanceof Function ? value(currentVal) : value;
        setLocalStorageItem(key, sanitize ? sanitize(valueToStore) : valueToStore);
        return valueToStore;
      });
    } catch (error) {
      logSSOTError(error, `useLocalStorage:write:${key}`);
    }
  }, [key, sanitize]);

  return [storedValue, setValue] as const;
}
