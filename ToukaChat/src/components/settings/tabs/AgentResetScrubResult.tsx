"use client";

import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";
import type { MemoryScrubReport, ScrubItemResult } from "@/lib/types";

type ScrubReason = NonNullable<ScrubItemResult["reason"]>;
type Translate = (key: string, options?: Record<string, unknown>) => string;

const FILE_KINDS: ReadonlyArray<ScrubItemResult["kind"]> = ["upgrade_backup", "snapshot"];
const CLEARED: ReadonlyArray<ScrubItemResult["status"]> = ["scrubbed", "not_needed"];

function reasonText(t: Translate, reason: ScrubReason | null | undefined): string {
    return reason ? t(`settings.agent_reset.scrub.reasons.${reason}`) : "";
}

/** 線上記憶庫與重查的那幾句（完成與否都要講清楚）. */
function liveNotes(t: Translate, report: MemoryScrubReport): string[] {
    const notes = new Set<string>();
    for (const live of (report.items ?? []).filter((item) => item.kind === "live_store" && item.status === "failed")) {
        // rewritten_after_reset 可能是整理前就發現（沒有整理），也可能是整理完才發現（舊審查報告丙-3），
        // 所以不說有沒有整理
        notes.add(
            live.reason === "live_verify_failed"
                ? t("settings.agent_reset.scrub.live_verify_failed")
                : live.reason === "rewritten_after_reset"
                  ? t("settings.agent_reset.scrub.live_rewritten")
                  : t("settings.agent_reset.scrub.live_not_compacted", { reason: reasonText(t, live.reason) }),
        );
    }
    if (report.ran && (report.memory_remaining ?? null) === null) notes.add(t("settings.agent_reset.scrub.remaining_unknown"));
    if ((report.memory_remaining ?? 0) > 0) notes.add(t("settings.agent_reset.scrub.remaining_positive"));
    return [...notes];
}

/** 還留著不在清除範圍（舊格式、舊前綴）資料的備份份數：每份備份一個資訊項，筆數在下方清單. */
export function outOfScopeCount(report: MemoryScrubReport | null | undefined): number {
    return (report?.items ?? []).filter(
        (item) => item.kind === "info" && item.reason === "out_of_scope" && (item.count ?? 0) > 0,
    ).length;
}

/** 總結句：沒完成時一律寫「沒有完成」加原因，不拿檔數下結論（類別沒跑完時檔數是錯的）.
 * 完成了但還有不在範圍的舊資料時，不說「每一份都已清除」（舊審查報告丙-3）。 */
function summary(t: Translate, report: MemoryScrubReport): string {
    if (!report.ran) return t("settings.agent_reset.scrub.summary_not_ran", { reason: reasonText(t, report.reason) });
    if (report.complete) {
        const left = outOfScopeCount(report);
        return left > 0
            ? t("settings.agent_reset.scrub.summary_complete_out_of_scope", { n: left })
            : t("settings.agent_reset.scrub.summary_complete");
    }
    const reason = report.reason ? reasonText(t, report.reason) : t("settings.agent_reset.scrub.reason_see_details");
    return t("settings.agent_reset.scrub.summary_unfinished", { reason });
}

/** 檔數那一句只在四類都跑完時才算得準. */
function filesLeft(t: Translate, report: MemoryScrubReport, files: ScrubItemResult[]): string | null {
    const allDone = (report.categories ?? []).every((category) => category.state === "done");
    const failed = files.filter((item) => item.status === "failed").length;
    if (report.complete || !allDone || failed === 0) return null;
    const done = files.filter((item) => CLEARED.includes(item.status)).length;
    return t("settings.agent_reset.scrub.files_left", { failed, done });
}

function itemLine(t: Translate, item: ScrubItemResult): string {
    return t(item.reason ? "settings.agent_reset.scrub.item_line_reason" : "settings.agent_reset.scrub.item_line", {
        name: item.name,
        status: t(`settings.agent_reset.scrub.statuses.${item.status}`),
        reason: reasonText(t, item.reason),
    });
}

/** 清除連備份的結果：總結句、線上庫與重查、沒跑完的類別、逐檔結果（含沒清掉的中斷暫存）、資訊項. */
export function AgentResetScrubResult({ report }: { report: MemoryScrubReport }) {
    const { t } = useTranslation();
    const items = report.items ?? [];
    const files = items.filter((item) => FILE_KINDS.includes(item.kind));
    const listed = items.filter(
        (item) => FILE_KINDS.includes(item.kind) || (item.kind === "leftover" && item.status === "failed"),
    );
    const infos = items.filter((item) => item.kind === "info");
    const unfinished = (report.categories ?? []).filter((category) => category.state !== "done");
    const left = filesLeft(t, report, files);

    return (
        <div className="space-y-1.5 rounded-xl border border-border bg-background/50 p-2">
            <p className={cn("font-bold", report.complete ? "text-status-success" : "text-status-warning")}>
                {summary(t, report)}
            </p>
            {left && <p className="font-bold text-status-warning">{left}</p>}
            {liveNotes(t, report).map((note) => (
                <p key={note} className="font-bold text-status-warning">
                    {note}
                </p>
            ))}
            {report.ran &&
                unfinished.map((category) => (
                    <p key={category.name} className="text-status-warning">
                        {category.state === "failed"
                            ? t("settings.agent_reset.scrub.category_failed", {
                                  category: t(`settings.agent_reset.scrub.categories.${category.name}`),
                                  reason: reasonText(t, category.reason),
                              })
                            : t("settings.agent_reset.scrub.category_not_started", {
                                  category: t(`settings.agent_reset.scrub.categories.${category.name}`),
                              })}
                    </p>
                ))}
            {listed.length > 0 && (
                <ul className="space-y-0.5 text-muted-foreground">
                    {listed.map((item) => (
                        <li key={`${item.kind}:${item.name}`} className={cn(item.status === "failed" && "text-status-warning")}>
                            {itemLine(t, item)}
                        </li>
                    ))}
                </ul>
            )}
            {infos.length > 0 && (
                <div className="text-muted-foreground">
                    <p className="font-bold">{t("settings.agent_reset.scrub.info_title")}</p>
                    <ul className="space-y-0.5">
                        {infos.map((item) => (
                            <li key={`${item.name}:${item.reason}`}>
                                {item.count == null
                                    ? t("settings.agent_reset.scrub.info_line_plain", {
                                          name: item.name,
                                          reason: reasonText(t, item.reason),
                                      })
                                    : t("settings.agent_reset.scrub.info_line", {
                                          name: item.name,
                                          reason: reasonText(t, item.reason),
                                          count: item.count,
                                      })}
                            </li>
                        ))}
                    </ul>
                </div>
            )}
        </div>
    );
}
