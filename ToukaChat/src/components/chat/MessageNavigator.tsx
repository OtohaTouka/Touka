"use client";

import React from 'react';
import { useTranslation } from 'react-i18next';
import { cn } from '@/lib/utils';
import { isTickCurrent, type NavTick } from '@/lib/chat/messageNavigation';

interface MessageNavigatorProps {
  ticks: NavTick[];
  /** 目前所在那則在線性路徑裡的位置。 */
  currentIndex: number | null;
  onJump: (index: number) => void;
}

interface PreviewState {
  key: string;
  /** 預覽中心對齊那條線，相對工具列的 px。 */
  top: number;
}

const MOVE_KEYS = new Set(['ArrowUp', 'ArrowDown', 'Home', 'End']);

function isKeyboardFocus(element: HTMLElement): boolean {
  try {
    return element.matches(':focus-visible');
  } catch {
    return false;
  }
}

/**
 * 對話左側的訊息導覽列（純展示）：一條線一則使用者訊息，移上去浮出預覽，點了跳過去。
 * 鍵盤用 roving tabindex：整排一個 Tab 停留點，上下鍵、Home、End 換條。
 */
export function MessageNavigator({ ticks, currentIndex, onJump }: MessageNavigatorProps) {
  const { t } = useTranslation();
  const [focusKey, setFocusKey] = React.useState<string | null>(null);
  const [preview, setPreview] = React.useState<PreviewState | null>(null);
  const buttons = React.useRef(new Map<string, HTMLButtonElement>());

  const currentKey = ticks.find(tick => isTickCurrent(tick, currentIndex))?.key ?? null;
  // 記住的焦點不在了（線變少、換分支）就退回目前那條，再退回第一條：永遠恰好一個 Tab 停留點
  const tabKey = ticks.some(tick => tick.key === focusKey) ? focusKey : (currentKey ?? ticks[0]?.key ?? null);
  const previewTick = preview ? ticks.find(tick => tick.key === preview.key) : undefined;

  const previewText = (tick: NavTick): string => {
    const { preview: text, attachment } = tick.entry;
    if (text) return text;
    if (attachment?.kind === 'image') return t('chat.message_nav.image');
    if (attachment) return t('chat.message_nav.attachment', { name: attachment.name ?? t('chat.message_nav.file') });
    return t('chat.message_nav.empty');
  };

  const labelOf = (tick: NavTick): string => (
    tick.firstOrdinal === tick.lastOrdinal
      ? t('chat.message_nav.jump_to', { n: tick.firstOrdinal, preview: previewText(tick) })
      : t('chat.message_nav.jump_to_range', {
          first: tick.firstOrdinal, last: tick.lastOrdinal, preview: previewText(tick),
        })
  );

  const showPreview = (element: HTMLButtonElement, key: string) => {
    setPreview({ key, top: element.offsetTop + element.offsetHeight / 2 });
  };
  const hidePreview = (key: string) => {
    setPreview(prev => (prev?.key === key ? null : prev));
  };

  // WCAG 1.4.13：預覽蓋住訊息時，不用移開滑鼠或焦點也能按 Esc 收起
  const previewShown = preview !== null;
  React.useEffect(() => {
    if (!previewShown) return;
    const dismiss = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setPreview(null);
    };
    document.addEventListener('keydown', dismiss);
    return () => document.removeEventListener('keydown', dismiss);
  }, [previewShown]);

  const handleKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (!MOVE_KEYS.has(event.key)) return;
    const position = ticks.findIndex(tick => buttons.current.get(tick.key) === event.target);
    if (position < 0) return;
    event.preventDefault();
    const last = ticks.length - 1;
    const next = event.key === 'Home' ? 0
      : event.key === 'End' ? last
      : event.key === 'ArrowUp' ? Math.max(position - 1, 0)
      : Math.min(position + 1, last);
    const key = ticks[next].key;
    const element = buttons.current.get(key);
    setFocusKey(key);
    if (!element) return;
    element.focus();
    showPreview(element, key);
  };

  return (
    <nav
      aria-label={t('chat.message_nav.label')}
      className="pointer-events-none absolute inset-y-0 left-0 z-30 flex w-6 items-center"
    >
      <div
        role="toolbar"
        aria-orientation="vertical"
        className="pointer-events-auto relative flex max-h-full w-full flex-col py-2"
        onKeyDown={handleKeyDown}
      >
        {ticks.map(tick => {
          const current = tick.key === currentKey;
          return (
            <button
              key={tick.key}
              type="button"
              ref={element => {
                if (element) buttons.current.set(tick.key, element);
                else buttons.current.delete(tick.key);
              }}
              tabIndex={tick.key === tabKey ? 0 : -1}
              aria-label={labelOf(tick)}
              aria-current={current ? 'true' : undefined}
              className="group flex h-4 min-h-0 w-full shrink items-center justify-center rounded-sm"
              onClick={() => {
                setPreview(null);
                onJump(tick.index);
              }}
              onMouseEnter={event => showPreview(event.currentTarget, tick.key)}
              onMouseLeave={() => hidePreview(tick.key)}
              onFocus={event => {
                setFocusKey(tick.key);
                if (isKeyboardFocus(event.currentTarget)) showPreview(event.currentTarget, tick.key);
              }}
              onBlur={() => hidePreview(tick.key)}
            >
              <span
                className={cn(
                  "block h-0.5 w-3 rounded-full transition-all group-hover:h-1 group-hover:w-5 group-focus-visible:h-1 group-focus-visible:w-5",
                  current ? "bg-primary" : "bg-muted-foreground",
                )}
              />
            </button>
          );
        })}
        {preview && previewTick && (
          <div
            aria-hidden="true"
            className="pointer-events-none absolute left-full ml-2 w-max max-w-xs -translate-y-1/2 rounded-lg border border-border bg-popover px-3 py-2 text-sm text-popover-foreground shadow-lg whitespace-pre-wrap break-words line-clamp-3"
            style={{ top: preview.top }}
          >
            {previewText(previewTick)}
          </div>
        )}
      </div>
    </nav>
  );
}
