"""Isolation CI：核對 Linux 隔離模組的 junit（只用標準函式庫，以 ``python -I`` 執行）.

用法::

    isolation_junit_check.py facts --probe probe.json --out facts.json
    isolation_junit_check.py check --pass {a,b} --junit junit.xml --facts facts.json

``facts``：讀這台主機的核心版本、LSM 清單、Fast Open／MPTCP 設定，加上 ``launcher.py --probe``
的輸出；判定網路設定會不會讓 Landlock 擋不住連線（和 ``launcher.net_sysctl_issue`` 同一套規則，
ABI≥4 才看，同自測），寫成 JSON 並印出。

``check``（ADR-0272，C2-2 設計 §4.1）：

- 兩個 pass：failed／error 都是 0；每個 skip 的訊息以 ``[skip:<標籤>] `` 開頭，而且標籤在這個
  pass 的允許清單內；subreaper 案例必須 passed；junit 記的 pytest pid 不是 1（pid 1 時孤兒本來
  就歸 pytest，「沒設 subreaper」抓不到）。
- pass A（不加 sysctl、不設 REQUIRE）：允許高於主機 ABI 的 ``abiN``，網路設定不安全時另允許
  ``net``，而且不安全分支的案例必須 passed。
- pass B（關掉 Fast Open／MPTCP、設 REQUIRE）：只允許高於主機 ABI 的 ``abiN``；主機 ABI 至少要到
  landlock 層；層級測試必須 passed。

exit 0 通過、1 不通過、2 用法或檔案錯誤。

輸入信任（S314 豁免的前提）：只在 CI 用。``check`` 只解析同一個 job 剛由 pytest 寫到
``$RUNNER_TEMP`` 的 junit：workflow 把 pytest 的 ``--junitxml`` 原樣傳給 ``--junit``，而且只有
``.github/workflows/isolation.yml`` 呼叫本腳本（由 ``tests/lint/test_isolation_ci_workflow.py``
釘住）。本腳本不在映像裡（``.dockerignore`` 排除 ``scripts/``），產品程式與代理人都呼叫不到。
不要拿它解析外部或代理人產生的 XML；需要時改用 ``defusedxml``。
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

# Linux 模組的 skip 標籤（單元測試核對：模組用到的正好是這些）
KNOWN_TAGS = frozenset({"platform", "landlock", "launcher", "net", "level", "abi4", "abi6", "root"})
TAG = re.compile(r"\[skip:([a-z0-9]+)\] ")
ABI_TAGS = {"abi4": 4, "abi6": 6}  # 要這個 ABI 才跑得了的案例
NET_TAG = "net"
NET_ABI = 4  # 有 TCP 規則才看網路設定（同 landlock.MIN_NET_ABI）
LANDLOCK_LEVEL_ABI = 4  # landlock 層要擋 TCP（同 isolation_policy.LANDLOCK_LAYER_MIN_ABI）
NET_SYSCTL_PATHS = ("/proc/sys/net/ipv4/tcp_fastopen", "/proc/sys/net/mptcp/enabled")
LSM_PATH = "/sys/kernel/security/lsm"
NET_TCP_FASTOPEN, NET_MPTCP = "net_tcp_fastopen", "net_mptcp"
PID_PROPERTY = "pytest_pid"

LEVEL_CASE = "test_ci_host_reaches_the_landlock_level"
# 原生 runner 上 pytest 不是 pid 1：setsid 出去的孤兒只能經 subreaper 回到執行器
SUBREAPER_CASES = (
    "test_setsid_daemon_is_killed_after_the_connection_ends[keep]",
    "test_setsid_daemon_is_killed_after_the_connection_ends[cleared]",
)
# 網路設定不安全時才走得到的分支（LOW 8）：pass A 必須真的跑過
UNSAFE_NET_CASES = (
    "test_self_test_reports_unsafe_net_sysctls_and_still_passes",
    "test_unsafe_net_sysctl_is_its_own_reason_not_fs_9p",
    "test_consented_fallback_keeps_file_isolation_when_net_sysctls_are_unsafe",
    "test_each_launch_follows_the_real_net_sysctls[net-strict]",
    "test_each_launch_follows_the_real_net_sysctls[consented-fallback]",
    # 執行器判 none、人核准後降級到執行器（features 不含 tcp）
    "test_terminal_command_runs_inside_the_executor",
    "test_executor_timeout_kills_the_remote_command",
    "test_verification_command_runs_inside_the_executor",
    "test_evolution_verify_runs_inside_the_executor",
)


@dataclass(frozen=True)
class Case:
    """一個 testcase 的結果：passed／skipped／failed／error。"""

    name: str
    outcome: str
    message: str = ""


@dataclass(frozen=True)
class Junit:
    """junit 裡要核對的部分。"""

    cases: tuple[Case, ...]
    properties: dict[str, str]
    suite_failures: int
    suite_errors: int


@dataclass(frozen=True)
class Facts:
    """主機的有效 ABI 與網路設定的判定（``None`` 是安全）。"""

    abi: int
    net: str | None


@dataclass(frozen=True)
class HostFacts:
    """直接從主機讀到的原始值（讀不到的設定是 ``None``）。"""

    kernel: str
    lsm: str
    fastopen: str | None
    mptcp: str | None


# ── facts ──────────────────────────────────────────────────────────────────────


def _sysctl_bits(text: str | None) -> int:
    try:
        return int(text) if text is not None else -1  # -1：每個位元都當成開著
    except ValueError:
        return -1


def net_issue(fastopen: str | None, mptcp: str | None) -> str | None:
    """同 ``launcher.net_sysctl_issue``：Fast Open 的 client 位元或 MPTCP 開著就擋不住連線。"""
    if _sysctl_bits(fastopen) & 1:
        return NET_TCP_FASTOPEN
    return NET_MPTCP if mptcp is not None and _sysctl_bits(mptcp) else None


def _read_text(path: str) -> str | None:
    """只讀；不存在回 None，其他讀取錯誤回空字串（看不懂，當成開著，同啟動器）。"""
    try:
        return Path(path).read_text(encoding="ascii")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        return ""


def read_host() -> HostFacts:
    """這台主機（這個網路命名空間）的原始值。"""
    fastopen, mptcp = (_read_text(path) for path in NET_SYSCTL_PATHS)
    return HostFacts(platform.release(), _read_text(LSM_PATH) or "", fastopen, mptcp)


def build_facts(probe: dict, host: HostFacts) -> dict:
    """``launcher.py --probe`` 的輸出加上主機的原始值；ABI≥4 才判定網路設定（同自測）。"""
    abi = probe.get("abi_effective")
    abi = abi if isinstance(abi, int) and not isinstance(abi, bool) else 0
    return {
        "kernel": host.kernel,
        "lsm": [name for name in host.lsm.strip().split(",") if name],
        "tcp_fastopen": None if host.fastopen is None else host.fastopen.strip(),
        "mptcp_enabled": None if host.mptcp is None else host.mptcp.strip(),
        "abi": abi,
        "net": net_issue(host.fastopen, host.mptcp) if abi >= NET_ABI else None,
        "probe": probe,
    }


def read_facts(path: Path) -> Facts:
    """``facts`` 寫的檔；欄位不對就丟 ``ValueError``。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    abi, net = data.get("abi"), data.get("net")
    if (
        not isinstance(abi, int)
        or isinstance(abi, bool)
        or not (net is None or isinstance(net, str))
    ):
        raise ValueError(path)
    return Facts(abi=abi, net=net)


# ── junit ──────────────────────────────────────────────────────────────────────


def _case(element: ET.Element) -> Case:
    name = element.get("name", "")
    for outcome, tag in (("error", "error"), ("failed", "failure"), ("skipped", "skipped")):
        child = element.find(tag)
        if child is not None:
            return Case(name, outcome, child.get("message", ""))
    return Case(name, "passed")


def read_junit(path: Path) -> Junit:
    """讀 pytest 寫的 junit（只給 CI：同一個 job 剛產生的檔，見模組說明的「輸入信任」）。"""
    root = ET.parse(path).getroot()  # noqa: S314 - 輸入可信、只在 CI：只解析同一個 job 剛由 pytest 產生的 junit
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    return Junit(
        cases=tuple(_case(case) for suite in suites for case in suite.iter("testcase")),
        properties={
            prop.get("name", ""): prop.get("value", "")
            for suite in suites
            for prop in suite.findall("properties/property")
        },
        suite_failures=sum(int(suite.get("failures", "0")) for suite in suites),
        suite_errors=sum(int(suite.get("errors", "0")) for suite in suites),
    )


# ── check ──────────────────────────────────────────────────────────────────────


def allowed_tags(stage: str, facts: Facts) -> frozenset[str]:
    """這個 pass 允許的 skip 標籤。"""
    tags = {tag for tag, abi in ABI_TAGS.items() if facts.abi < abi}
    if stage == "a" and facts.net:
        tags.add(NET_TAG)
    return frozenset(tags)


def required_cases(stage: str, facts: Facts) -> tuple[str, ...]:
    """這個 pass 必須 passed 的案例。"""
    cases = list(SUBREAPER_CASES)
    if stage == "a" and facts.net:
        cases += UNSAFE_NET_CASES
    if stage == "b":
        cases.append(LEVEL_CASE)
    return tuple(cases)


def _tag(case: Case) -> str | None:
    match = TAG.match(case.message)
    return match[1] if match else None


def check(junit: Junit, facts: Facts, stage: str) -> list[str]:
    """不通過的理由（空清單＝通過）。"""
    problems = []
    if not junit.cases:
        problems.append("junit has no test cases")
    if junit.suite_failures or junit.suite_errors:
        problems.append(f"testsuite: {junit.suite_failures} failures, {junit.suite_errors} errors")
    allowed = allowed_tags(stage, facts)
    for case in junit.cases:
        if case.outcome in {"failed", "error"}:
            problems.append(f"{case.name}: {case.outcome}: {case.message}")
        elif case.outcome == "skipped" and _tag(case) not in allowed:
            problems.append(f"{case.name}: skip not allowed in pass {stage}: {case.message}")
    passed = {case.name for case in junit.cases if case.outcome == "passed"}
    problems += [
        f"{name}: must pass in pass {stage}"
        for name in required_cases(stage, facts)
        if name not in passed
    ]
    pid = junit.properties.get(PID_PROPERTY, "")
    if not pid.isdigit() or int(pid) == 1:
        problems.append(f"pytest pid is {pid or 'not recorded'}: run pytest natively, not as pid 1")
    if stage == "b" and facts.abi < LANDLOCK_LEVEL_ABI:
        problems.append(f"Landlock ABI {facts.abi} < {LANDLOCK_LEVEL_ABI}: no landlock level here")
    return problems


def _summary(junit: Junit, stage: str) -> str:
    outcomes = Counter(case.outcome for case in junit.cases)
    tags = Counter(_tag(case) or "untagged" for case in junit.cases if case.outcome == "skipped")
    counts = ", ".join(f"{outcomes[o]} {o}" for o in ("passed", "skipped", "failed", "error"))
    by_tag = ", ".join(f"{tag}×{n}" for tag, n in sorted(tags.items())) or "none"
    return f"pass {stage}: {counts}; skips by tag: {by_tag}"


# ── 指令列 ─────────────────────────────────────────────────────────────────────


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    facts = commands.add_parser("facts")
    facts.add_argument("--probe", type=Path, required=True)
    facts.add_argument("--out", type=Path, required=True)
    checks = commands.add_parser("check")
    checks.add_argument("--pass", dest="stage", choices=("a", "b"), required=True)
    checks.add_argument("--junit", type=Path, required=True)
    checks.add_argument("--facts", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """指令列入口。"""
    args = _parser().parse_args(argv)
    if args.command == "facts":
        facts = build_facts(json.loads(args.probe.read_text(encoding="utf-8")), read_host())
        text = json.dumps(facts, ensure_ascii=False, sort_keys=True)
        args.out.write_text(text + "\n", encoding="utf-8")
        sys.stdout.write(text + "\n")
        return 0
    try:
        junit, facts = read_junit(args.junit), read_facts(args.facts)
    except (OSError, ValueError, ET.ParseError) as exc:
        sys.stdout.write(f"cannot read the junit or the facts: {exc!r}\n")
        return 2
    problems = check(junit, facts, args.stage)
    sys.stdout.write(_summary(junit, args.stage) + "\n")
    for problem in problems:
        sys.stdout.write(f"FAIL {problem}\n")
    sys.stdout.write("OK\n" if not problems else f"{len(problems)} problem(s)\n")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
