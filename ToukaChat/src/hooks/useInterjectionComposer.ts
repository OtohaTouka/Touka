/**
 * ADR-0249 第 4a 段：輸入框在 AI 工作中的行為（DB-20）。
 *
 * - Enter＝排隊、Ctrl／Cmd+Enter＝優先排隊（第 4b 段：立即中斷正在寫的回答）、Esc＝停止；沒在生成時 Enter 照舊送出。
 *   （@ 選單開著時 Esc 由 MentionInput 先關選單，不會傳到這裡。）
 * - 回合結束：正常完成 → 未送達的合成下一則自動送出；按停止或出錯 → 放回輸入框，不自動送出。
 * - 切到別的對話不算回合結束（停止旗標歸零）；背景對話的未送達項目留在佇列，回來時可收回或修改。
 * - 自動送出掛在剛答完的 assistant 節點下（handleSend 的 parentIdOverride），不用尚未更新的舊樹。
 */
import { useCallback, useEffect, useRef } from 'react';
import type React from 'react';
import { useStreamRegistry } from '@/lib/providers/StreamRegistryProvider';
import { useInterjectionStore, type QueuedInterjection } from '@/lib/chat/interjectionStore';
import { composerKeyAction, joinUndeliveredText } from '@/lib/chat/interjections';
import { useInterjectionQueue } from './useInterjectionQueue';

interface ComposerParams {
  sessionId: string;
  isSending: boolean;
  disabled: boolean;
  inputValue: string;
  setInputValue: (value: string) => void;
  send: (text?: string, parentId?: string | null) => Promise<void>;
  stop: () => void;
}

const appendText = (current: string, text: string) => (current ? `${current}\n${text}` : text);

export function useInterjectionComposer(params: ComposerParams) {
  const { sessionId, isSending, disabled, inputValue, setInputValue, send, stop } = params;
  const stream = useStreamRegistry().getStream(sessionId);
  const queue = useInterjectionQueue({
    sessionId, streamId: stream?.started?.stream_id, requestScope: stream?.requestScope ?? null,
  });
  const stoppedRef = useRef(false);
  const latest = useRef({ inputValue, setInputValue, send, failed: false, sending: isSending, sessionId });
  const lastAssistant = useRef<{ sessionId: string; id: string } | null>(null);
  useEffect(() => {
    latest.current = { inputValue, setInputValue, send, failed: stream?.status === 'error', sending: isSending,
      sessionId };
    if (stream?.assistantMsgId) lastAssistant.current = { sessionId, id: stream.assistantMsgId };
  });

  const queueInput = useCallback((urgent = false) => {
    const text = inputValue.trim();
    if (!text || disabled) return;
    setInputValue('');
    void queue.enqueue(text, urgent).then(item => {
      // hook 擋下（ADR-0292，沒有加入佇列）：放回還在這個對話的輸入框。
      if (!item) {
        if (latest.current.sessionId === sessionId) latest.current.setInputValue(appendText(latest.current.inputValue, text));
        return;
      }
      // 回應到時這個對話已不在生成中（404，或 201 晚於回合結束）：放回輸入框，不留在佇列等下一輪。
      // 已切到別的對話：留在原對話的佇列，不放進別的對話的輸入框。
      if (latest.current.sessionId !== sessionId || latest.current.sending) return;
      useInterjectionStore.getState().remove(sessionId, item.id);
      latest.current.setInputValue(appendText(latest.current.inputValue, item.text));
    });
  }, [inputValue, disabled, setInputValue, queue, sessionId]);

  const stopGeneration = useCallback(() => {
    stoppedRef.current = true;
    stop();
  }, [stop]);

  const onKeyDown = useCallback((e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    const action = composerKeyAction(e, isSending);
    if (!action) return;
    e.preventDefault();
    if (disabled) return;
    if (action === 'send') void send();
    else if (action === 'stop') stopGeneration();
    else queueInput(action === 'queue_urgent');
  }, [isSending, disabled, send, stopGeneration, queueInput]);

  const edit = useCallback(async (item: QueuedInterjection) => {
    if (await queue.retract(item)) setInputValue(appendText(latest.current.inputValue, item.text));
  }, [queue, setInputValue]);

  // 回合結束（同一對話 isSending true → false）
  const previous = useRef({ sessionId, isSending });
  useEffect(() => {
    const was = previous.current;
    previous.current = { sessionId, isSending };
    if (was.sessionId !== sessionId) {
      stoppedRef.current = false;  // 上一個對話按的停止不帶到這個對話
      return;
    }
    if (!was.isSending || isSending) return;
    const interrupted = stoppedRef.current || latest.current.failed;
    stoppedRef.current = false;
    const text = joinUndeliveredText(useInterjectionStore.getState().take(sessionId));
    if (!text) return;
    if (interrupted) latest.current.setInputValue(appendText(latest.current.inputValue, text));
    else {
      const parentId = lastAssistant.current?.sessionId === sessionId ? lastAssistant.current.id : undefined;
      void latest.current.send(text, parentId);
    }
  }, [sessionId, isSending]);

  return { items: queue.items, onKeyDown, queueInput, stop: stopGeneration, retract: queue.retract, edit };
}
