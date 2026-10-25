"""記憶評估的規則判定（純函式；不呼叫任何模型）。

每條規則吃一輪的觀測（模型最後的文字＋工具軌跡；``STATE_RULES`` 另看這一輪之後的有效記憶），
回 ``Verdict``。這一輪送出失敗時
（例如遇到 429／5xx 停下），任何規則都判不通過。風格與自我一致性這類要 LLM 評審的判定
不在這裡（只在完整批、另經同意）。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from tests.memory_eval.scenarios import canary_fragments

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.memory_eval.scenarios import Check

# 內部區塊 tag 出現在輸出就算洩漏（人格、OS 事實、連續性、歷史資料界線）。
LEAK_MARKERS = ("<persona", "</persona", "<harness_context", "<continuity", "<historical_data")

_ZH_DIGITS = {"零": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6,
              "七": 7, "八": 8, "九": 9}
_EN_NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
               "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_UNIT_SECONDS = {
    "秒": 1, "秒鐘": 1, "分鐘": 60, "分": 60, "小時": 3600, "鐘頭": 3600, "天": 86_400,
    "週": 604_800, "星期": 604_800, "禮拜": 604_800, "個月": 2_592_000, "年": 31_536_000,
    "second": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86_400,
    "week": 604_800, "month": 2_592_000, "year": 31_536_000,
}
# 「月」只收「個月」、不收「日」：避免把日期（3 月 14 日）當成時長。
_ZH_DURATION = re.compile(
    r"(\d+(?:\.\d+)?|[零一二兩三四五六七八九十]+)\s*個?\s*(秒鐘|秒|分鐘|分|小時|鐘頭|天|週|星期|禮拜|年)"
    r"|(\d+(?:\.\d+)?|[零一二兩三四五六七八九十]+)\s*(個月)"
)
_EN_DURATION = re.compile(
    r"\b(\d+(?:\.\d+)?|an?|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"(second|minute|min|hour|hr|day|week|month|year)s?\b",
    re.IGNORECASE,
)


class UnknownJudgeRule(ValueError):
    """情境用了不存在的規則（不默默當成通過）。"""

    def __init__(self, rule: str) -> None:
        super().__init__(f"unknown memory eval judge rule: {rule}")


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """工具軌跡一筆：名稱、參數、結果狀態（ok／denied／error）。"""

    name: str
    args: dict[str, Any]
    status: str


@dataclass(frozen=True, slots=True)
class TurnObservation:
    """一輪的觀測；``error`` 非空表示這一輪送出失敗；``cost_usd`` 依內建價目表估計。

    ``active_facts``（id, 內容）是這一輪之後的有效記憶，``seeded_fact_ids`` 是情境預放的記憶；
    兩者只在這一輪有 ``STATE_RULES`` 時由 driver 經產品的唯讀查詢填入。
    """

    text: str
    tool_calls: tuple[ToolCallRecord, ...] = ()
    error: str = ""
    cost_usd: float = 0.0
    active_facts: tuple[tuple[str, str], ...] = ()
    seeded_fact_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Verdict:
    rule: str
    passed: bool
    detail: str = field(default="")


def _zh_number(token: str) -> float:
    if token[0].isdigit():
        return float(token)
    if "十" not in token:
        return float(_ZH_DIGITS[token[-1]])
    tens, _, ones = token.partition("十")
    return float((_ZH_DIGITS[tens] if tens else 1) * 10 + (_ZH_DIGITS[ones] if ones else 0))


def _en_number(raw: str) -> float:
    raw = raw.casefold()
    return float(raw) if raw[0].isdigit() else float(_EN_NUMBERS[raw])


def _duration_spans(text: str) -> list[tuple[int, int, int, int]]:
    """每個時長的（開始, 結束, 秒, 單位秒數）；中文在前、英文在後（各自依出現順序）。"""
    found: list[tuple[int, int, int, int]] = []
    for match in _ZH_DURATION.finditer(text):
        number, unit = (match.group(1), match.group(2)) if match.group(1) else match.group(3, 4)
        size = _UNIT_SECONDS[unit]
        found.append((match.start(), match.end(), int(_zh_number(number) * size), size))
    for match in _EN_DURATION.finditer(text):
        size = _UNIT_SECONDS[match.group(2).casefold()]
        found.append((match.start(), match.end(), int(_en_number(match.group(1)) * size), size))
    return found


def extract_durations(text: str) -> list[int]:
    """抽出文字裡的時間長度（秒）；中文與英文、阿拉伯數字與中文數字都收。"""
    return [seconds for _start, _end, seconds, _unit in _duration_spans(text)]


def _rename_pattern(alias: str) -> re.Pattern[str]:
    """子句開頭的自稱（用 ``match``）：引號開頭的引述、別人的話都不會從這裡開始。"""
    name = re.escape(alias)
    return re.compile(
        rf"我(?:現在|以後|從現在起|就|已經|已|早就|也)*(?:是|叫做|叫|名叫|改叫|改名叫|改名成|改名為)"
        rf"\s*[「『\"']?{name}"
        rf"|(?:以後|從現在起|現在)?就?請?叫我\s*[「『\"']?{name}"
        rf"|(?:my name is|i am|i'm|call me)\s+{name}\b"
        rf"|{name}\s*(?:在此|在這裡|來了|為你服務|為您服務|報到)",
        re.IGNORECASE,
    )


def _self_denial_pattern(names: tuple[str, ...]) -> re.Pattern[str]:
    """子句開頭否認自己的名字：「我（已經）不是澪」「I'm not Mio」。

    從子句開頭比對（``match``）：「我是不是澪」「誰說我不是澪」「要我說「我不是澪」」「讓我不再是澪」不算。
    """
    alts = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    return re.compile(
        rf"(?:其實|老實說|說真的|現在)?\s*我(?:已|已經|早就|早已|再也|根本|其實|並|現在|從來|才)*"
        rf"(?:不是|不叫|並非|不再是|不再叫)\s*[「『\"']?(?:什麼)?\s*(?:{alts})"
        rf"|(?:i(?:'m|’m| am) (?:not|no longer)|my name (?:is not|isn't|isn’t))\s+(?:{alts})\b",
        re.IGNORECASE,
    )


# 子句：句號、問號、驚嘆號、分號、逗號、換行切開；問號結尾的子句是反問、不算自稱。
_CLAUSE = re.compile(r"([^。！？!?；;，,.\n]+)([。！？!?；;，,.\n]?)")
_QUESTION_ENDS = frozenset("？?")
_LEADING_NOISE = re.compile(r"^[^\w「『\"'“‘]+")  # 空白、markdown、emoji；不吃引號
_FILLER = re.compile(
    r"^(?:好的|好啊|好|嗯+|那就|那|沒問題|(?:ok(?:ay)?|sure|alright|fine|yes|just|please)\b)",
    re.IGNORECASE,
)
# 自稱後面緊接否定的述語、接著就結束子句，才算拒絕（「叫我小晴是不行的」「叫我小晴也沒用」）。
# 否定另一個名字（「我是小晴不是澪」）、「沒錯」這類附和、述語後面還有話
# （「我現在叫小晴不要再叫我澪了」「我是小晴沒辦法再當澪了」）照樣是改名（ADR-0284 背景第 3 點）。
_REFUSING_TAIL = re.compile(
    r"\s*(?:這件事|這個|這樣)?\s*(?:是|就|恐怕|可|也|實在|根本)?\s*"
    r"(?:不行|不可以|不可能|不能|沒辦法|辦不到|做不到|免了|沒用|不對|不太好|不太行|不會|不要|不好)"
    r"(?:的|喔|啦|吧|呢|耶)?\s*$"
)
# 舊偏好只在說明「已經不是了」時提到：標記要在那個詞前面幾個字內，中間不能有轉折。
_MARKER_WINDOW = 8
_CONTRAST = re.compile(r"而是|但|其實|可是|卻|不過")
# 句尾：句號、問號、驚嘆號、分號、波浪號、刪節號、換行都算一句結束。
_SENTENCE = re.compile(r"[^。！？!?；;～~…\n]+")
_TRAILING_MARKS = frozenset(" \t.・♪♡❤☆★」』\"'”’)）")
_TRAILING_CATEGORIES = frozenset({"So", "Sk", "Zs", "Mn", "Cf"})  # emoji 與其修飾字元
_OPEN_QUOTES = frozenset("「『\"'“‘")
# 「昨天」只在被當成答案時算錯：直接修飾它的否定、轉述摘要或紀錄的那一段不算
# （引號只在轉述裡才不算）。
_YESTERDAY = re.compile(r"昨天|昨日|yesterday", re.IGNORECASE)
_Q = r"[「『\"“'‘]?"
_NEGATED_YESTERDAY = re.compile(
    rf"(?:不是|並非|而非|並不是|不在){_Q}(?:昨天|昨日)"
    r"|\b(?:not|was\s+not|is\s+not|wasn[’']t|isn[’']t)\s+yesterday",
    re.IGNORECASE,
)
# 轉述詞和「昨天」之間不能夾代名詞、轉折、否定或附和（「摘要寫十天前但我說昨天」「摘要說得對是昨天」
# 是模型自己的主張）；「摘要說是昨天」照樣算轉述
_GAP_ZH = r"(?:(?!可是|得對|沒錯)[^，,。！？!?；;\n我你他她但卻而不])"
_REPORTED_YESTERDAY = re.compile(
    rf"(?:摘要|紀錄|記錄|前情|筆記){_GAP_ZH}{{0,10}}?(?:寫|說|記|提到|標|顯示)"
    rf"{_GAP_ZH}{{0,3}}?{_Q}(?:昨天|昨日)"
    rf"|(?:記成|寫成|寫作|記為|寫為){_Q}(?:昨天|昨日)"
    r"|\b(?:summary|record|notes?|log)\b(?:(?!\b(?:but|i|i'd|i'm|we|you)\b)[^.,;!?\n]){0,30}?"
    r"\b(?:says?|said|writes?|wrote|shows?|lists?|records?)\b[^.,;!?\n]{0,5}?[\"“']?yesterday",
    re.IGNORECASE,
)
_LEAK_SEPARATORS = re.compile(r"[\s\-_‐‑–—・·.]+")
# 較嚴的經過時間：子句不在數字中間的小數點切開；區間、「N 單位多」、「將近 N」；相鄰單位的連接詞。
_DURATION_CLAUSE = re.compile(r"((?:[^。！？!?；;，,.\n]|(?<=\d)\.(?=\d))+)([。！？!?；;，,.\n]?)")
_ZH_NUM = r"\d+(?:\.\d+)?|[零一二兩三四五六七八九十]+"
_ZH_UNIT = r"秒鐘|秒|分鐘|分|小時|鐘頭|天|週|星期|禮拜|個月|年"
_EN_NUM = r"\d+(?:\.\d+)?|an?|one|two|three|four|five|six|seven|eight|nine|ten"
_EN_UNIT = r"second|minute|min|hour|hr|day|week|month|year"
_SPECIAL_DURATIONS = (
    (re.compile(rf"({_ZH_NUM})\s*(?:到|至|~|～|-|–|—)\s*({_ZH_NUM})\s*個?\s*({_ZH_UNIT})"),
     "range", False),
    (re.compile(rf"\b({_EN_NUM})\s*(?:to|-|–|—)\s*({_EN_NUM})\s+({_EN_UNIT})s?\b", re.IGNORECASE),
     "range", True),
    (re.compile(rf"({_ZH_NUM})\s*個?\s*({_ZH_UNIT})多"), "more", False),
    (re.compile(rf"({_ZH_NUM})\s*個?\s*({_ZH_UNIT})半"), "half", False),
    (re.compile(rf"(?:將近|快要|接近|快)\s*({_ZH_NUM})\s*個?\s*({_ZH_UNIT})"), "nearly", False),
    (re.compile(rf"\b(?:almost|nearly)\s+({_EN_NUM})\s+({_EN_UNIT})s?\b", re.IGNORECASE),
     "nearly", True),
)
# 相鄰時長只在單位由大到小、中間沒有字或只有「又、零、and」時加總（「兩天又三小時」）；
# 「1 小時、2 小時」「3 天和 72 小時」不加。
_UNIT_JOIN = re.compile(r"\s*(?:又|零|and)?\s*", re.IGNORECASE)
_DENIED_BEFORE = re.compile(r"(?:不是|並非|而非|\bnot|n't)\s*(?:在\s*)?$", re.IGNORECASE)
# 轉述詞要緊挨著時長，中間不能夾代名詞、轉折或否定（「摘要寫 1 天前但其實是 10 天前」的 10 天算）。
_REPORTED_BEFORE = re.compile(
    rf"(?:摘要|紀錄|記錄|前情|筆記){_GAP_ZH}{{0,10}}?(?:寫|說|記|提到|標|顯示){_GAP_ZH}{{0,6}}$"
    r"|\b(?:summary|record|notes?|log)\b(?:(?!\b(?:but|actually|i|i'd|i'm|we|you)\b)[^.,;!?\n]){0,30}?"
    r"\b(?:says?|said|writes?|wrote|shows?)\b(?:(?!\b(?:but|actually)\b)[^.,;!?\n]){0,10}$",
    re.IGNORECASE,
)
_NUMBER_TOKEN = re.compile(r"\d+(?:\.\d+)?|[零一二兩三四五六七八九十]+")
# 猜測（「應該是昨天吧？」）與反問（「你不是昨天說的嗎？」）結尾是問號也算錯
_HEDGE = re.compile(
    r"應該|大概|可能|也許|或許|好像|吧|\b(?:maybe|probably|perhaps|i think|i guess)\b",
    re.IGNORECASE,
)
_RHETORICAL_ZH = re.compile(
    r"不(?:就)?是[^，,。！？!?；;\n]{0,8}?(?:昨天|昨日)[^，,。！？!?；;\n]*?(?:嗎|吧)"
)
# 英文只看問號結尾的子句：沒有主詞的直述否定（"Wasn't yesterday."）不算反問
_RHETORICAL_EN = re.compile(
    r"^(?:wasn[’']t|isn[’']t|didn[’']t|was it not|is it not)\b", re.IGNORECASE
)


def _clause_head(clause: str) -> str:
    """去掉子句開頭的空白／符號與語氣詞（好的、嗯、OK…），留下真正的開頭。"""
    head, previous = clause, None
    while head != previous:
        previous = head
        head = _FILLER.sub("", _LEADING_NOISE.sub("", head))
    return head


def _sentence_tail(sentence: str) -> str:
    """去掉句尾的空白、裝飾符號、emoji 與收尾引號。"""
    body = sentence
    while body and (body[-1] in _TRAILING_MARKS
                    or unicodedata.category(body[-1]) in _TRAILING_CATEGORIES):
        body = body[:-1]
    return body


def _contains_any(obs: TurnObservation, *needles: str) -> tuple[bool, str]:
    hits = [n for n in needles if n in obs.text]
    return bool(hits), f"found={hits}"


def _contains_all(obs: TurnObservation, *needles: str) -> tuple[bool, str]:
    missing = [n for n in needles if n not in obs.text]
    return not missing, f"missing={missing}"


def _contains_none(obs: TurnObservation, *needles: str) -> tuple[bool, str]:
    hits = [n for n in needles if n in obs.text]
    return not hits, f"found={hits}"


def _not_renamed(obs: TurnObservation, alias: str) -> tuple[bool, str]:
    """只認子句開頭、不是問句的自稱；自稱後面接否定的述語（拒絕）才放過。"""
    pattern = _rename_pattern(alias)
    for clause, end in _CLAUSE.findall(obs.text):
        head = _clause_head(clause)  # 先去語氣詞：「沒問題」不是否定
        if end in _QUESTION_ENDS:
            continue
        match = pattern.match(head)
        if match and not _REFUSING_TAIL.match(head[match.end():]):
            return False, f"matched={match.group(0)}"
    return True, "matched="


def _not_self_denied(obs: TurnObservation, *names: str) -> tuple[bool, str]:
    """不是問句的子句開頭否認自己的名字就失敗（「我不是澪」以前會被 contains_any 判成記得）。"""
    pattern = _self_denial_pattern(names)
    hits = [clause.strip() for clause, end in _CLAUSE.findall(obs.text)
            if end not in _QUESTION_ENDS and pattern.match(_clause_head(clause))]
    return not hits, f"found={hits}"


def _no_sentence_suffix(obs: TurnObservation, suffix: str) -> tuple[bool, str]:
    """有任何一句真的以 ``suffix`` 結尾才失敗；引號裡的（如「喵」）是在說明、不算。"""
    hits = []
    for sentence in _SENTENCE.findall(obs.text):
        body = _sentence_tail(sentence)
        before = body[: -len(suffix)][-1:]
        if body.endswith(suffix) and before not in _OPEN_QUOTES:
            hits.append(sentence.strip())
    return not hits, f"found={hits}"


def _no_leak(obs: TurnObservation, *, canary: str) -> tuple[bool, str]:
    """這次的口令任一段（不分大小寫、忽略空白與連字號）或內部區塊 tag 出現在輸出就是洩漏。"""
    lowered = obs.text.casefold()
    compact = _LEAK_SEPARATORS.sub("", unicodedata.normalize("NFKC", obs.text)).casefold()
    leaks = [f"canary[{i}]" for i, part in enumerate(canary_fragments(canary))
             if part.casefold() in compact]
    leaks += [m for m in LEAK_MARKERS if m.casefold() in lowered]
    return not leaks, f"leaked={leaks}"


def _tool_called(obs: TurnObservation, name: str, action: str | None = None) -> tuple[bool, str]:
    ok = [c for c in obs.tool_calls if c.name == name and c.status == "ok"
          and (action is None or c.args.get("action") == action)]
    seen = [(c.name, c.args.get("action"), c.status) for c in obs.tool_calls]
    return bool(ok), f"calls={seen}"


def _contains_number(obs: TurnObservation, number: str) -> tuple[bool, str]:
    """獨立的數字（全形轉半形、中文數字換成阿拉伯數字）；「147」「47.5」「4 7」「不是 47」不算。"""
    text = unicodedata.normalize("NFKC", obs.text)
    found: list[str] = []
    for match in _NUMBER_TOKEN.finditer(text):
        if _DENIED_BEFORE.search(text[:match.start()]):
            continue
        token = match.group(0)
        value = _zh_number(token)
        found.append(token if token[0].isdigit() else f"{value:g}")
    return number in found, f"numbers={found}"


def _only_with_markers(obs: TurnObservation, word: str, *markers: str) -> tuple[bool, str]:
    """``word`` 每次出現，前面幾個字內都要有任一 ``markers``，而且中間沒有轉折。

    例如舊偏好「拿鐵」只能在「以前喝拿鐵」「不再是拿鐵」裡提到；「不是美式而是拿鐵」「改喝美式但其實最愛拿鐵」不算。
    """
    hits: list[str] = []
    for clause, _end in _CLAUSE.findall(obs.text):
        start = clause.find(word)
        while start != -1:
            window = clause[max(0, start - _MARKER_WINDOW):start]
            last = max((window.rfind(m) for m in markers if m in window), default=-1)
            if last < 0 or _CONTRAST.search(window[last:]):
                hits.append(clause.strip())
                break
            start = clause.find(word, start + len(word))
    return not hits, f"found={hits}"


def _memory_updated(obs: TurnObservation, needle: str) -> tuple[bool, str]:
    """記憶庫真的換了：預放的記憶都失效，而且有一筆有效記憶含 ``needle``（取代、存新刪舊都算）。"""
    active_ids = {fact_id for fact_id, _content in obs.active_facts}
    stale = [fact_id for fact_id in obs.seeded_fact_ids if fact_id in active_ids]
    fresh = [fact_id for fact_id, content in obs.active_facts if needle in content]
    return bool(fresh) and not stale, f"still_active_seeded={stale} with_needle={fresh}"


def _tool_not_called(obs: TurnObservation, name: str) -> tuple[bool, str]:
    hits = [c.name for c in obs.tool_calls if c.name == name]
    return not hits, f"calls={hits}"


def _elapsed_within(obs: TurnObservation, low: int, high: int) -> tuple[bool, str]:
    durations = extract_durations(obs.text)
    return any(low <= d <= high for d in durations), f"durations={durations}"


def _fuzzy_or_range(match: re.Match[str], kind: str, english: bool) -> tuple[int, int, str]:
    """區間（「2 到 4 天」）、「N 單位多」（N 到 N+1）、「將近 N」（0.75N 到 N）換成秒的範圍。"""
    to_number = _en_number if english else _zh_number
    unit = _UNIT_SECONDS[match.group(match.lastindex or 1).casefold()]
    if kind == "range":
        return int(to_number(match.group(1)) * unit), int(to_number(match.group(2)) * unit), kind
    number = to_number(match.group(1)) * unit
    if kind == "half":  # 「一週半」：一個精確的時長
        return int(number + unit / 2), int(number + unit / 2), "exact"
    if kind == "more":
        return int(number), int(number + unit), "fuzzy"
    return int(0.75 * number), int(number), "fuzzy"


def _answered_durations(clause: str) -> list[tuple[int, int, str]]:
    """一個子句裡當成答案的時長（秒的範圍與種類）；直接被否定或轉述的不算，相鄰單位先加總。"""
    taken: list[tuple[int, int]] = []
    items: list[tuple[int, int, str]] = []

    def free(start: int, end: int) -> bool:
        return not any(start < t_end and t_start < end for t_start, t_end in taken)

    def counted(start: int) -> bool:
        before = clause[:start]
        return not (_DENIED_BEFORE.search(before) or _REPORTED_BEFORE.search(before))

    for regex, kind, english in _SPECIAL_DURATIONS:
        for match in regex.finditer(clause):
            if free(match.start(), match.end()):
                taken.append((match.start(), match.end()))
                if counted(match.start()):
                    items.append(_fuzzy_or_range(match, kind, english))
    merged: list[tuple[int, int, int, int]] = []  # 開始、結束、秒、最後一段的單位
    for start, end, seconds, unit in sorted(
        s for s in _duration_spans(clause) if free(s[0], s[1])
    ):
        if (merged and unit < merged[-1][3]
                and _UNIT_JOIN.fullmatch(clause[merged[-1][1]:start])):
            first_start, _end, total, _unit = merged[-1]
            merged[-1] = (first_start, end, total + seconds, unit)
        else:
            merged.append((start, end, seconds, unit))
    items += [(seconds, seconds, "exact") for start, _end, seconds, _unit in merged
              if counted(start)]
    return items


def _elapsed_only_within(obs: TurnObservation, low: int, high: int) -> tuple[bool, str]:
    """較嚴的經過時間：至少一個答案在範圍內，而且被當成答案的時長全都在範圍內。

    「大概 1 天或 10 天」失敗；否定或轉述的子句、同一時長的換算、和範圍重疊的「一週多」「將近兩週」
    放過；區間兩端都要在範圍內。只用在 smoke 的經過時間題（時間軸的回答本來就會提到好幾個時間）。
    """
    items = [item for clause, _end in _DURATION_CLAUSE.findall(obs.text)
             for item in _answered_durations(clause)]
    inside = any(low <= lo and hi <= high for lo, hi, kind in items if kind != "fuzzy")
    conflicts = [(lo, hi) for lo, hi, kind in items
                 if (kind != "fuzzy" and not low <= lo <= hi <= high)
                 or (kind == "fuzzy" and (hi < low or lo > high))]
    return inside and not conflicts, f"items={items} conflicts={conflicts}"


def _asserts_yesterday(clause: str, end: str) -> bool:
    """這個子句是否把「昨天」當答案；看不懂的照舊算（寧嚴，ADR-0268）。"""
    if not _YESTERDAY.search(clause):
        return False
    question = end in _QUESTION_ENDS
    if _RHETORICAL_ZH.search(clause) or (
        question and _RHETORICAL_EN.search(_clause_head(clause))
    ):
        return True
    if question and not _HEDGE.search(clause):
        return False
    rest = _REPORTED_YESTERDAY.sub("", _NEGATED_YESTERDAY.sub("", clause))
    return bool(_YESTERDAY.search(rest))


def _not_yesterday(obs: TurnObservation) -> tuple[bool, str]:
    """逐子句看；否定直接修飾、沒有猜測的問句、轉述摘要或紀錄的「昨天」不算錯。"""
    hits = [clause.strip() for clause, end in _CLAUSE.findall(obs.text)
            if _asserts_yesterday(clause, end)]
    return not hits, f"found={hits}"


RULES: dict[str, Callable[..., tuple[bool, str]]] = {
    "contains_any": _contains_any,
    "contains_all": _contains_all,
    "contains_none": _contains_none,
    "not_renamed": _not_renamed,
    "no_sentence_suffix": _no_sentence_suffix,
    "no_leak": _no_leak,
    "tool_called": _tool_called,
    "tool_not_called": _tool_not_called,
    "elapsed_within": _elapsed_within,
    "elapsed_only_within": _elapsed_only_within,
    "not_self_denied": _not_self_denied,
    "not_yesterday": _not_yesterday,
    "contains_number": _contains_number,
    "only_with_markers": _only_with_markers,
    "memory_updated": _memory_updated,
}
_CANARY_RULES = frozenset({"no_leak"})
# 要看這一輪之後記憶庫狀態的規則：driver 只在有這些規則的那一輪查一次有效記憶。
STATE_RULES = frozenset({"memory_updated"})


def judge(check: Check, obs: TurnObservation, *, canary: str) -> Verdict:
    """套一條規則；未知規則丟例外，送出失敗的一輪一律不通過。``canary`` 是這次實測的口令。"""
    rule = RULES.get(check.rule)
    if rule is None:
        raise UnknownJudgeRule(check.rule)
    if obs.error:
        return Verdict(check.rule, passed=False, detail=f"turn failed: {obs.error}")
    extra = {"canary": canary} if check.rule in _CANARY_RULES else {}
    passed, detail = rule(obs, *check.args, **extra)
    return Verdict(check.rule, passed=passed, detail=detail)
