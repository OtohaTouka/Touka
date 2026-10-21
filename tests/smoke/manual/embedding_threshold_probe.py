"""人工 probe：用合成成對句子量嵌入模型的分數分布，校準語意查重門檻與查詢下限。

這不是自動 smoke：pytest、CI、guard 與 run_all_smoke_tests 都不跑它。不載入任何 Touka 程式
（src、memory_server），不讀任何金鑰檔、不送請求給 LLM 廠商；網路只連 Hugging Face 下載模型。
快取全部放在 --cache-root 底下，跑完由操作者整個刪除。執行前先把 Hugging Face 的快取指過去：

    HF_HOME=<cache-root> SENTENCE_TRANSFORMERS_HOME=<cache-root>/sentence-transformers \
    HF_HUB_DISABLE_TELEMETRY=1 \
    python tests/smoke/manual/embedding_threshold_probe.py --cache-root <cache-root> \
        [--human-approved-model-download] [--output <report.md>]

沒帶 --human-approved-model-download 時只讀快取，快取沒有就停（下載約 1.13 GB，每次要人類同意）。
只抓 sentence-transformers 要用的 safetensors 那一套（MODEL_FILES），釘在 MODEL_REVISION。

分數照產品的配對方式算（``memory_server.vector``）：
- 查重：新句子加 ``query: ``、舊記憶加 ``passage: ``，cosine；分數「大於」門檻就擋。
- 查詢：問題加 ``query: ``、記憶加 ``passage: ``，cosine；分數低於下限的不回傳。
前綴由 ``tests/unit/test_embedding_thresholds.py`` 核對和產品相同。

選法（寧可漏擋也不要誤擋；寧可多回傳也不要漏掉有關的）：
- 查重門檻＝誤擋為 0 的最低格（0.005 一格）再加 DEDUP_MARGIN。
- 查詢下限＝有關的（direct＋related）全部留下的最高格（0.01 一格）再減 FLOOR_MARGIN。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import huggingface_hub
import numpy as np
import sentence_transformers
import torch
import transformers
from huggingface_hub import HfApi, snapshot_download
from huggingface_hub import constants as hf_constants
from sentence_transformers import SentenceTransformer
from transformers.utils.hub import HF_MODULES_CACHE

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence


MODEL_ID = "intfloat/multilingual-e5-base"
MODEL_REVISION = "d128750597153bb5987e10b1c3493a34e5a4502a"
# sentence-transformers 從本機資料夾載入要用的檔：只要 safetensors 一套
# （不抓 pytorch_model.bin、onnx/、openvino/）
MODEL_FILES = (
    "1_Pooling/config.json",
    "config.json",
    "model.safetensors",
    "modules.json",
    "sentence_bert_config.json",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
)
MAX_DOWNLOAD_BYTES = 1_200_000_000
DOWNLOAD_FLAG = "--human-approved-model-download"
# 產品的 e5 前綴（memory_server.vector._format_for_embedding）
QUERY_PREFIX = "query: "
PASSAGE_PREFIX = "passage: "

DATA_FILE = Path(__file__).resolve().parents[2] / "fixtures" / "embedding_threshold_pairs.json"
CATEGORIES = ("equivalent", "update", "contradiction", "related", "unrelated")
LANGS = ("zh", "en", "mixed")
CROSS_TOPIC = "cross_topic"  # 對「其他主題」的舊記憶
NOT_DUPLICATE = ("update", "contradiction", "related", "unrelated", CROSS_TOPIC)
QUERY_BUCKETS = ("direct", "related", "unrelated", CROSS_TOPIC)
RELEVANT = ("direct", "related")
IRRELEVANT = ("unrelated", CROSS_TOPIC)
CURRENT_DUPLICATE_THRESHOLD = 0.92
CURRENT_QUERY_FLOOR = 0.3
DEDUP_GRID = tuple(round(0.800 + 0.005 * step, 3) for step in range(40))  # 0.800..0.995
FLOOR_GRID = tuple(round(0.30 + 0.01 * step, 2) for step in range(66))  # 0.30..0.95
DEDUP_MARGIN = 0.01
FLOOR_MARGIN = 0.05
LISTED = 5  # 邊界附近列出幾筆實際句子

Pair = tuple[float, str]  # (分數, 哪一組的哪兩句)
Scores = dict[str, dict[str, list[Pair]]]  # 類別 -> 語言 -> 配對


class ProbeStopped(SystemExit):
    """probe 在下載或量測前停下（原因寫在訊息裡）."""


def _stop(reason: str) -> NoReturn:
    raise ProbeStopped(reason)


def _write(text: str) -> None:
    sys.stdout.write(text + "\n")


def _require_cache_inside(cache_root: Path) -> None:
    """Hugging Face 會寫入的位置都必須在 --cache-root 底下，否則不下載就停."""
    root = cache_root.resolve()
    locations = {
        "HF_HOME": hf_constants.HF_HOME,
        "HF_HUB_CACHE": hf_constants.HF_HUB_CACHE,
        "HF_ASSETS_CACHE": hf_constants.HF_ASSETS_CACHE,
        "HF_XET_CACHE": hf_constants.HF_XET_CACHE,
        "HF_MODULES_CACHE": HF_MODULES_CACHE,
    }
    outside = [
        name for name, value in locations.items() if not Path(value).resolve().is_relative_to(root)
    ]
    if outside:
        _stop(f"{', '.join(outside)} 不在 --cache-root 底下；先設 HF_HOME={root} 再跑")
    if not hf_constants.HF_HUB_DISABLE_TELEMETRY:
        _stop("先設 HF_HUB_DISABLE_TELEMETRY=1 再跑")


def _planned_sizes() -> dict[str, int]:
    """下載前向 Hugging Face 查這一版要抓的檔與大小；合計超過上限就停."""
    info = HfApi(token=False).model_info(MODEL_ID, revision=MODEL_REVISION, files_metadata=True)
    sizes = {
        sibling.rfilename: int(sibling.size or 0)
        for sibling in info.siblings or []
        if sibling.rfilename in MODEL_FILES
    }
    missing = sorted(set(MODEL_FILES) - set(sizes))
    if missing:
        _stop(f"這一版 repo 沒有 {missing}")
    total = sum(sizes.values())
    if total > MAX_DOWNLOAD_BYTES:
        _stop(f"預計下載 {total:,} bytes，超過上限 {MAX_DOWNLOAD_BYTES:,}")
    _write(f"預計下載 {len(sizes)} 個檔，合計 {total:,} bytes")
    return sizes


def _fetch_model(cache_root: Path, *, approved: bool) -> tuple[Path, dict[str, int]]:
    """只抓 MODEL_FILES（沒有同意時只讀快取）；回傳 snapshot 資料夾與實際檔案大小."""
    planned = _planned_sizes() if approved else {}
    snapshot = Path(
        snapshot_download(
            repo_id=MODEL_ID,
            revision=MODEL_REVISION,
            allow_patterns=list(MODEL_FILES),
            cache_dir=hf_constants.HF_HUB_CACHE,
            local_files_only=not approved,
            token=False,
        )
    ).resolve()
    if not snapshot.is_relative_to(cache_root.resolve()):
        _stop(f"snapshot 不在 --cache-root 底下：{snapshot}")
    actual = {
        path.relative_to(snapshot).as_posix(): path.stat().st_size
        for path in snapshot.rglob("*")
        if path.is_file()
    }
    if set(actual) != set(MODEL_FILES):
        _stop(f"snapshot 的檔案和預期不同：{sorted(actual)}")
    changed = sorted(name for name, size in planned.items() if actual[name] != size)
    if changed:
        _stop(f"下載的大小和 API 不同：{changed}")
    return snapshot, actual


def _load_groups() -> list[dict]:
    return json.loads(DATA_FILE.read_text(encoding="utf-8"))["groups"]


def _encode(model: SentenceTransformer, texts: Sequence[str], prefix: str) -> np.ndarray:
    """單位長度向量：內積就是 cosine（產品的 Qdrant collection 用 COSINE）."""
    return model.encode(
        [f"{prefix}{text}" for text in texts],
        batch_size=32,
        convert_to_numpy=True,
        normalize_embeddings=True,
        show_progress_bar=False,
    )


def _empty_scores(categories: Iterable[str]) -> Scores:
    return {category: {lang: [] for lang in LANGS} for category in categories}


def _add(scores: Scores, bucket: str, lang: str, similarity: np.ndarray, labels: list[str]) -> None:
    scores[bucket][lang] += list(zip(similarity.ravel().tolist(), labels, strict=True))


def _dedup_scores(model: SentenceTransformer, groups: list[dict], new_prefix: str) -> Scores:
    """查重配對：新句子（new_prefix）對舊記憶（passage:）.

    同組的分到該句的類別；對其他組的舊記憶一律算 cross_topic（存記憶時比的是全部舊記憶）。
    """
    memories = _encode(model, [group["memory"] for group in groups], PASSAGE_PREFIX)
    scores = _empty_scores((*CATEGORIES, CROSS_TOPIC))
    for index, group in enumerate(groups):
        others = [other for position, other in enumerate(groups) if position != index]
        for category in CATEGORIES:
            texts = group[category]
            similarity = _encode(model, texts, new_prefix) @ memories.T
            own = [f"{group['id']}「{text}」→「{group['memory']}」" for text in texts]
            _add(scores, category, group["lang"], similarity[:, index], own)
            crossed = [
                f"{group['id']}「{text}」→ {other['id']}" for text in texts for other in others
            ]
            _add(scores, CROSS_TOPIC, group["lang"], np.delete(similarity, index, axis=1), crossed)
    return scores


def _query_scores(model: SentenceTransformer, groups: list[dict]) -> Scores:
    """查詢配對：問題（query:）對記憶（passage:）.

    direct＝同組的舊記憶與等同／更新／矛盾句（都直接回答問題）；related＝同組的相關句；
    unrelated＝同組的無關句；cross_topic＝其他組的舊記憶。
    """
    memories = _encode(model, [group["memory"] for group in groups], PASSAGE_PREFIX)
    scores = _empty_scores(QUERY_BUCKETS)
    for index, group in enumerate(groups):
        queries = group["queries"]
        encoded = _encode(model, queries, QUERY_PREFIX)
        direct = [group["memory"], *group["equivalent"], *group["update"], *group["contradiction"]]
        for bucket, texts in (
            ("direct", direct),
            ("related", group["related"]),
            ("unrelated", group["unrelated"]),
        ):
            labels = [f"{group['id']}「{query}」→「{text}」" for query in queries for text in texts]
            similarity = encoded @ _encode(model, texts, PASSAGE_PREFIX).T
            _add(scores, bucket, group["lang"], similarity, labels)
        others = [other for position, other in enumerate(groups) if position != index]
        labels = [
            f"{group['id']}「{query}」→ {other['id']}" for query in queries for other in others
        ]
        crossed = np.delete(encoded @ memories.T, index, axis=1)
        _add(scores, CROSS_TOPIC, group["lang"], crossed, labels)
    return scores


def _pairs(scores: Scores, buckets: Iterable[str], lang: str | None = None) -> list[Pair]:
    return [
        pair
        for bucket in buckets
        for pair_lang, pairs in scores[bucket].items()
        if lang in (None, pair_lang)
        for pair in pairs
    ]


def _values(scores: Scores, buckets: Iterable[str], lang: str | None = None) -> list[float]:
    return [score for score, _label in _pairs(scores, buckets, lang)]


def _five_numbers(values: Sequence[float]) -> str:
    q1, median, q3 = statistics.quantiles(values, n=4, method="inclusive")
    return f"{min(values):.3f} | {q1:.3f} | {median:.3f} | {q3:.3f} | {max(values):.3f}"


def _distribution_table(scores: Scores) -> list[str]:
    lines = [
        "| 類別 | 語言 | 筆數 | 最小 | Q1 | 中位數 | Q3 | 最大 |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for bucket in scores:
        for lang in (*LANGS, None):
            values = _values(scores, [bucket], lang)
            name = lang or "all"
            lines.append(f"| {bucket} | {name} | {len(values)} | {_five_numbers(values)} |")
    return lines


def _rate(hits: int, total: int) -> str:
    return f"{hits}/{total} ({hits / total:.1%})" if total else "0/0"


def _blocked(values: Sequence[float], threshold: float) -> int:
    return sum(score > threshold for score in values)


def _kept(values: Sequence[float], floor: float) -> int:
    return sum(score >= floor for score in values)


def _with_current(grid: tuple[float, ...], current: float) -> list[tuple[float, str]]:
    return [(value, "（現值）" if value == current else "") for value in sorted({*grid, current})]


def _dedup_sweep(scores: Scores) -> tuple[list[str], float]:
    """每個門檻的漏擋（等同句沒被擋）與誤擋（不是重複卻被擋）；回傳誤擋為 0 的最低格."""
    positives = _values(scores, ["equivalent"])
    names = " | ".join(f"誤擋 {name}" for name in NOT_DUPLICATE)
    lines = [
        f"| 門檻 | 漏擋 equivalent | {names} | 精確率 | 召回率 |",
        "|---" * (len(NOT_DUPLICATE) + 4) + "|",
    ]
    clean = [
        value for value in DEDUP_GRID if _blocked(_values(scores, NOT_DUPLICATE), value) == 0
    ]
    for threshold, label in _with_current(DEDUP_GRID, CURRENT_DUPLICATE_THRESHOLD):
        caught = _blocked(positives, threshold)
        cells, false_total = [], 0
        for name in NOT_DUPLICATE:
            values = _values(scores, [name])
            false_total += _blocked(values, threshold)
            cells.append(_rate(_blocked(values, threshold), len(values)))
        precision = caught / (caught + false_total) if caught + false_total else 1.0
        lines.append(
            f"| {threshold:.3f}{label} | {_rate(len(positives) - caught, len(positives))} "
            f"| {' | '.join(cells)} | {precision:.3f} | {caught / len(positives):.3f} |"
        )
    return lines, min(clean)


def _floor_sweep(scores: Scores) -> tuple[list[str], float]:
    """每個下限留下多少有關的、放進多少無關的；回傳有關的全部留下的最高格."""
    lines = [
        "| 下限 | 留下 direct | 留下 related | 放進 unrelated | 放進 cross_topic |",
        "|---|---|---|---|---|",
    ]
    lowest_relevant = min(_values(scores, RELEVANT))
    for floor, label in _with_current(FLOOR_GRID, CURRENT_QUERY_FLOOR):
        cells = [
            _rate(_kept(_values(scores, [name]), floor), len(_values(scores, [name])))
            for name in QUERY_BUCKETS
        ]
        lines.append(f"| {floor:.2f}{label} | {' | '.join(cells)} |")
    return lines, max(value for value in FLOOR_GRID if value <= lowest_relevant)


def _listed(pairs: list[Pair], *, highest: bool) -> list[str]:
    ordered = sorted(pairs, key=lambda pair: pair[0], reverse=highest)[:LISTED]
    return [f"  - {score:.3f} {label}" for score, label in ordered]


def _per_language(scores: Scores, buckets: Sequence[str], hit: object) -> str:
    cells = []
    for lang in (*LANGS, None):
        values = _values(scores, buckets, lang)
        cells.append(f"{lang or 'all'} {_rate(sum(map(hit, values)), len(values))}")
    return "；".join(cells)


def _dedup_section(dedup: Scores, symmetric: Scores) -> list[str]:
    sweep, clean = _dedup_sweep(dedup)
    chosen = round(clean + DEDUP_MARGIN, 3)
    symmetric_clean = _dedup_sweep(symmetric)[1]

    def blocks(value: float, threshold: float = chosen) -> bool:
        return value > threshold

    return [
        "### 查重分數分布（新句子 query: 對舊記憶 passage:）",
        *_distribution_table(dedup),
        "",
        "### 查重門檻掃描（分數 > 門檻就擋）",
        *sweep,
        "",
        f"- 誤擋為 0 的最低格：{clean:.3f}；"
        f"建議門檻＝{clean:.3f}＋{DEDUP_MARGIN}＝**{chosen:.3f}**",
        f"- 建議門檻下擋下等同句：{_per_language(dedup, ['equivalent'], blocks)}",
        f"- 建議門檻下誤擋：{_per_language(dedup, NOT_DUPLICATE, blocks)}",
        f"- 對照（不是產品行為）：兩邊都用 passage: 時，誤擋為 0 的最低格 {symmetric_clean:.3f}，"
        "那時擋下等同句："
        + _per_language(symmetric, ["equivalent"], lambda value: value > symmetric_clean),
        "- 不是重複但分數最高的幾筆：",
        *_listed(_pairs(dedup, NOT_DUPLICATE), highest=True),
        "- 等同句分數最低的幾筆：",
        *_listed(_pairs(dedup, ["equivalent"]), highest=False),
    ]


def _query_section(query: Scores) -> list[str]:
    sweep, keep_all = _floor_sweep(query)
    chosen = round(keep_all - FLOOR_MARGIN, 2)

    def kept(value: float) -> bool:
        return value >= chosen

    return [
        "### 查詢分數分布（問題 query: 對記憶 passage:）",
        *_distribution_table(query),
        "",
        "### 查詢下限掃描（分數 ≥ 下限才回傳）",
        *sweep,
        "",
        f"- 有關的全部留下的最高格：{keep_all:.2f}；建議下限＝{keep_all:.2f}－{FLOOR_MARGIN}"
        f"＝**{chosen:.2f}**",
        f"- 建議下限下留下 direct：{_per_language(query, ['direct'], kept)}",
        f"- 建議下限下留下 related：{_per_language(query, ['related'], kept)}",
        f"- 建議下限下放進無關：{_per_language(query, IRRELEVANT, kept)}",
        "- 有關的分數最低的幾筆：",
        *_listed(_pairs(query, RELEVANT), highest=False),
        "- 無關的分數最高的幾筆：",
        *_listed(_pairs(query, IRRELEVANT), highest=True),
    ]


def _report(model: SentenceTransformer, groups: list[dict], files: dict[str, int]) -> list[str]:
    counts = "、".join(f"{lang} {sum(g['lang'] == lang for g in groups)}" for lang in LANGS)
    return [
        f"模型 `{MODEL_ID}`＠`{MODEL_REVISION}`；裝置 CPU；"
        f"sentence-transformers {sentence_transformers.__version__}、"
        f"transformers {transformers.__version__}、torch {torch.__version__}、"
        f"huggingface_hub {huggingface_hub.__version__}。",
        f"snapshot 檔案（{len(files)} 個，合計 {sum(files.values()):,} bytes）：",
        *[f"- `{name}`：{size:,}" for name, size in sorted(files.items())],
        f"資料：{len(groups)} 組（{counts}）。",
        "",
        *_dedup_section(
            _dedup_scores(model, groups, QUERY_PREFIX), _dedup_scores(model, groups, PASSAGE_PREFIX)
        ),
        "",
        *_query_section(_query_scores(model, groups)),
    ]


def main(argv: Sequence[str] | None = None) -> int:
    """解析參數、檢查快取位置、取得模型、印出報告."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cache-root", required=True, type=Path)
    parser.add_argument(DOWNLOAD_FLAG, dest="approved", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    _require_cache_inside(args.cache_root)
    snapshot, files = _fetch_model(args.cache_root, approved=args.approved)
    model = SentenceTransformer(
        str(snapshot),
        device="cpu",
        cache_folder=str(args.cache_root.resolve() / "sentence-transformers"),
        local_files_only=True,
        trust_remote_code=False,
    )
    report = "\n".join(_report(model, _load_groups(), files))
    _write(report)
    if args.output is not None:
        args.output.write_text(report + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
