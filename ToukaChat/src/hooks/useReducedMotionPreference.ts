import { useTheme } from '@/lib/ThemeContext';
import { useMediaQuery } from './useMediaQuery';

/**
 * 這個人要不要減少動態：App 設定優先，「跟隨系統」才看系統。
 * CSS 只能把動畫停掉；停了之後要改用文字表示狀態，所以畫面也要知道。
 */
export function useReducedMotionPreference(): boolean {
  const { reduceMotion } = useTheme();
  const systemPrefersReduced = useMediaQuery('(prefers-reduced-motion: reduce)');
  return reduceMotion === 'on' || (reduceMotion === 'system' && systemPrefersReduced);
}
