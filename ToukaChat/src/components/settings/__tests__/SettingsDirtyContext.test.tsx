import { describe, it, expect } from "vitest";
import { useEffect, type ReactNode } from "react";
import { render, act } from "@testing-library/react";
import {
    useSettingsDirtyStore,
    SettingsDirtyProvider,
    useDirtyGuard,
    type SettingsDirtyStore,
} from "../SettingsDirtyContext";

function Guarded({ k, dirty }: { k: string; dirty: boolean }) {
    useDirtyGuard(k, dirty);
    return null;
}

/** 測試載具：建立 store、暴露給測試（storeRef）、並以 Provider 下放給 children。 */
function Harness({
    storeRef,
    children,
}: {
    storeRef: { current: SettingsDirtyStore | null };
    children: ReactNode;
}) {
    const store = useSettingsDirtyStore();
    // 在 effect 中（非 render 期）暴露 store——避免 react-hooks/refs 規則。store 為穩定 memo 值。
    useEffect(() => {
        storeRef.current = store;
    }, [store, storeRef]);
    return <SettingsDirtyProvider store={store}>{children}</SettingsDirtyProvider>;
}

describe("SettingsDirtyContext (C5 未存變更守衛)", () => {
    it("彙整 children 的 dirty，卸載時自動清除", () => {
        const ref = { current: null as SettingsDirtyStore | null };
        const { rerender } = render(
            <Harness storeRef={ref}>
                <Guarded k="a" dirty={false} />
            </Harness>,
        );
        expect(ref.current!.isAnyDirty()).toBe(false);

        // 變 dirty → isAnyDirty 為 true
        rerender(
            <Harness storeRef={ref}>
                <Guarded k="a" dirty={true} />
            </Harness>,
        );
        expect(ref.current!.isAnyDirty()).toBe(true);

        // 卸載該 guarded child → 其 dirty 位被清除（切分頁後草稿已不存在）
        rerender(<Harness storeRef={ref}>{null}</Harness>);
        expect(ref.current!.isAnyDirty()).toBe(false);
    });

    it("多個來源獨立計數；clearAll 一次清空（捨棄並切換後）", () => {
        const ref = { current: null as SettingsDirtyStore | null };
        render(
            <Harness storeRef={ref}>
                <Guarded k="a" dirty={true} />
                <Guarded k="b" dirty={true} />
            </Harness>,
        );
        expect(ref.current!.isAnyDirty()).toBe(true);

        act(() => ref.current!.clearAll());
        expect(ref.current!.isAnyDirty()).toBe(false);
    });

    it("一個 dirty、一個乾淨 → 仍視為 dirty；乾淨者回報 false 不影響", () => {
        const ref = { current: null as SettingsDirtyStore | null };
        render(
            <Harness storeRef={ref}>
                <Guarded k="a" dirty={false} />
                <Guarded k="b" dirty={true} />
            </Harness>,
        );
        expect(ref.current!.isAnyDirty()).toBe(true);
    });

    it("無 Provider 時 useDirtyGuard 為 no-op，不丟例外", () => {
        expect(() => render(<Guarded k="a" dirty={true} />)).not.toThrow();
    });
});
