import { useMediaQuery } from "./useMediaQuery";

/** 手機版版面的判斷條件（page.tsx 的版面切換與 ChatArea 的訊息導覽列共用）。 */
const MOBILE_MEDIA_QUERY = "(max-width: 768px)";

export function useIsMobile(): boolean {
  return useMediaQuery(MOBILE_MEDIA_QUERY);
}
