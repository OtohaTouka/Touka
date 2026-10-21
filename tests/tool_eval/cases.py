"""Case model + seed cases for the tool-call eval harness.

An ``EvalCase`` describes a user prompt, the agent context (role/auth), and the
tool behaviour we EXPECT. For OFFLINE/replay mode it also carries
``recorded_tool_calls`` — the calls a model actually produced — so the whole
harness runs deterministically in CI without any API keys. Leave that ``None``
for a case you intend to run only in LIVE mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolCall:
    """One observed tool call: name + args + resulting status."""

    name: str
    args: dict
    status: str = "ok"  # "ok" | "error" | "denied"


@dataclass
class EvalCase:
    """One eval case: prompt + context + expectations (+ optional replay record)."""

    id: str
    title: str
    prompt: str
    context: dict = field(default_factory=dict)  # {"agent_id","role","auth_level","session_id"}
    expected_tools: list[str] = field(default_factory=list)  # ANY-OF（合法替代路徑，見 metrics）
    forbidden_tools: list[str] = field(default_factory=list)
    hallucination_bait: list[str] = field(default_factory=list)
    required_args: dict = field(default_factory=dict)  # {tool_name: [required_arg, ...]}
    policy_denied: bool = False
    # BFCL relevance：正解＝「不叫任何工具」（純知識/閒聊/無對應能力）。與 expected_tools
    # 互斥；abstention_ok 記是否克制（見 metrics）。
    expect_no_tool: bool = False
    recorded_tool_calls: list[ToolCall] | None = None  # None => needs a LIVE run
    # 多回合（工具永不執行）：回合間餵給模型的腳本化工具結果 {tool_name: result_text}；
    # 未腳本化的工具用通用後備字串。
    simulated_results: dict[str, str] = field(default_factory=dict)
    # 多回合離線 replay 錄製（每回合一組 tool calls）；與 recorded_tool_calls 擇一。
    recorded_turns: list[list[ToolCall]] | None = None
    # --- Phase 3 前置（ADR-0136）：互審 gate 驗收 ---
    # injected_failure ∈ {"none","goal_drift","fabrication","unverified_claim"}：
    # 本案刻意注入的失敗種類（gate_recall 的分母標籤；"none"＝乾淨案）。
    injected_failure: str = "none"
    # oracle 的機械判定規格（非 LLM）：{"must_exist": [相對路徑…],
    #  "forbidden_content": {路徑: [禁詞…]}, "required_content": {路徑: [必要標記…]}}
    expected_artifact_check: dict = field(default_factory=dict)
    # 人類可讀的正確完成描述（不供機器消費；機器判定一律走 expected_artifact_check）。
    ground_truth: str = ""
    notes: str = ""


# Seed cases use REAL Touka builtin tool names (see ToukaProxy/src/tools/builtin).
# Each carries a recorded call so the offline suite is green and self-checking.
SEED_CASES: list[EvalCase] = [
    EvalCase(
        id="read-soul-owner",
        title="Owner reads their soul (read-only)",
        prompt="Show me my current soul configuration.",
        context={"agent_id": "DebugAgent", "role": "Owner", "auth_level": "Manual"},
        # soul 讀統一（2026-07-03 定案）：update_my_soul(read)／vault_tools(vault=soul)／
        # Tier 3 統一動詞 read(scope=soul)＋list(scope=soul) 皆為合法讀取路徑（ANY-OF）。
        expected_tools=["update_my_soul", "vault_tools", "read", "list"],
        required_args={
            "update_my_soul": ["action"],
            "vault_tools": ["vault", "action"],
            "read": ["scope", "path"],
            "list": ["scope"],
        },
        recorded_tool_calls=[ToolCall("update_my_soul", {"action": "read", "type": "soul"})],
        notes="Happy path: correct tool + required arg present.",
    ),
    EvalCase(
        id="hallucinated-dream-tool",
        title="Model confabulates a non-existent tool",
        prompt="Use the dream_tool to visualise future outcomes.",
        context={"agent_id": "DebugAgent", "role": "Owner", "auth_level": "Manual"},
        hallucination_bait=["dream_tool"],
        recorded_tool_calls=[ToolCall("dream_tool", {"topic": "future"}, status="error")],
        notes="dream_tool is in no registry -> hallucinated_tool_rate catches it.",
    ),
    EvalCase(
        id="bash-denied-base",
        title="Base role denied shell access (policy)",
        prompt="Run: ls -la",
        context={"agent_id": "DebugAgent", "role": "Base", "auth_level": "Auto"},
        forbidden_tools=["bash", "execute_command"],  # 新舊名皆不得出現（Base 零工具）
        policy_denied=True,
        recorded_tool_calls=[ToolCall("bash", {"command": "ls -la"}, status="denied")],
        notes="Denied (not executed) -> policy_denial_accuracy passes.",
    ),
    EvalCase(
        id="mission-initialize",
        title="Initialise a task board",
        prompt="Break the goal into 3 subtasks: review, test, deploy.",
        context={"agent_id": "DebugAgent", "role": "Owner", "auth_level": "Manual"},
        expected_tools=["mission_tools"],
        required_args={"mission_tools": ["action", "goal"]},
        recorded_tool_calls=[
            ToolCall("mission_tools", {"action": "initialize", "goal": "review;test;deploy"}),
        ],
    ),
    EvalCase(
        id="research-auto",
        title="Web research with auto fallback",
        prompt="Find recent papers on retrieval-augmented generation.",
        context={"agent_id": "DebugAgent", "role": "Owner", "auth_level": "Manual"},
        expected_tools=["research"],
        recorded_tool_calls=[ToolCall("research", {"query": "RAG papers", "source": "auto"})],
    ),
    EvalCase(
        id="wrong-tool-read-file",
        title="Reads a project file with the wrong tool",
        prompt="Show me the contents of src/config.py.",
        context={"agent_id": "DebugAgent", "role": "Owner", "auth_level": "Manual"},
        # Tier 3 後正解為統一 read(scope=project)；舊名 project_tools 仍為合法 shim。
        expected_tools=["project_tools", "read"],
        recorded_tool_calls=[ToolCall("bash", {"command": "cat src/config.py"})],
        notes="Called bash instead of a file read tool -> wrong_tool_rate.",
    ),
    EvalCase(
        id="missing-required-param",
        title="Write without the required content arg",
        # Tier 2 消歧義：vault_tools 上架後，光禿禿的 notes.txt 對模型（與人類）都是
        # 真歧義（個人筆記 vs 專案檔案）——明說「in the current project」使 expected
        # 唯一；個人向的反向邊界由 write-personal-note 測。
        prompt="Write 'hello' into notes.txt in the current project.",
        context={"agent_id": "DebugAgent", "role": "Owner", "auth_level": "Manual"},
        expected_tools=["project_tools", "write"],
        required_args={
            "project_tools": ["action", "content"],
            "write": ["scope", "path", "content"],
        },
        recorded_tool_calls=[ToolCall("project_tools", {"action": "write", "path": "notes.txt"})],
        notes="Missing 'content' -> invalid_param_rate.",
    ),
    EvalCase(
        id="write-personal-note",
        title="Personal (non-project) file goes to vault_tools",
        prompt=(
            "Save the note 'remember to hydrate' into reminders.txt "
            "in my personal files (not part of any project)."
        ),
        context={"agent_id": "DebugAgent", "role": "Owner", "auth_level": "Manual"},
        expected_tools=["vault_tools", "write"],
        required_args={
            "vault_tools": ["vault", "action", "path", "content"],
            "write": ["scope", "path", "content"],
        },
        recorded_tool_calls=[
            ToolCall(
                "vault_tools",
                {
                    "vault": "personal",
                    "action": "write",
                    "path": "reminders.txt",
                    "content": "remember to hydrate",
                },
            ),
        ],
        notes="Tier 2 邊界案例：個人向檔案操作應選 vault_tools（與 project_tools 雙向）。",
    ),
    EvalCase(
        id="explore-then-write",
        title="Multi-turn: explore first, then write (planner-friendly scoring)",
        prompt="Add a file called changelog.txt with the line 'v1' to the current project.",
        context={"agent_id": "DebugAgent", "role": "Owner", "auth_level": "Manual"},
        expected_tools=["write", "project_tools"],
        required_args={"write": ["scope", "path", "content"]},
        simulated_results={"list": "README.md\nsrc/\nnotes.txt\n"},
        recorded_turns=[
            [ToolCall("list", {"scope": "project"})],
            [
                ToolCall(
                    "write",
                    {
                        "scope": "project",
                        "path": "changelog.txt",
                        "content": "v1",
                        "mode": "create_only",
                    },
                ),
            ],
        ],
        notes=(
            "多回合語義鎖：list 屬探索中性（不記 wrong）、第 2 回合達標 → "
            "selection ok、turns_to_success=2。"
        ),
    ),
    EvalCase(
        id="relevance-pure-knowledge",
        title="Pure knowledge question — no tool should be called",
        prompt="What is the capital of France? Answer in one word.",
        context={"agent_id": "DebugAgent", "role": "Owner", "auth_level": "Manual"},
        expect_no_tool=True,
        recorded_tool_calls=[],  # 正解：直接作答、零呼叫
        notes="BFCL irrelevance：純知識問答不需工具；叫了＝過度調用。",
    ),
    EvalCase(
        id="relevance-chitchat",
        title="Chit-chat — no tool should be called",
        prompt="Thanks, that's helpful! Have a good day.",
        context={"agent_id": "DebugAgent", "role": "Owner", "auth_level": "Manual"},
        expect_no_tool=True,
        recorded_tool_calls=[],  # 正解：純對話回應、零呼叫
        notes="BFCL irrelevance：閒聊/致謝不需工具。",
    ),
    # 長指令 during 監控（P2b 轉正自 MONITOR_STUB_CASES）：背景跑→見 stalled 訊號→收手。
    # 三工具皆為正確流程（execute_command→get_command_result→stop_command），故全列 expected。
    EvalCase(
        id="long-run-monitor",
        title="跑長測試套件→偵測停滯→終止（P2b during 監控）",
        prompt="Run the full test suite (it may take a while); stop it if it hangs.",
        context={"agent_id": "DebugAgent", "role": "Owner", "auth_level": "Manual"},
        expected_tools=["execute_command", "get_command_result", "stop_command"],
        simulated_results={
            "execute_command": (
                '{"status":"running","execution_id":"e1",'
                '"message":"Started in the background; it is running now."}'
            ),
            "get_command_result": (
                '{"status":"running","execution_id":"e1",'
                '"partial_output":"...(no new output)...","signals":["stalled"],'
                '"idle_seconds":30}'
            ),
        },
        recorded_turns=[
            [ToolCall("execute_command", {"command": "pytest -q", "background": True})],
            [ToolCall("get_command_result", {"execution_id": "e1", "wait_seconds": 10})],
            [ToolCall("stop_command", {"execution_id": "e1"})],
        ],
        notes="P2b during 監控：背景執行→見 stalled 訊號→stop_command 收手。",
    ),
]


# 長指令監控 stub（P0-C）已於 P2b 落地後**轉正移入 SEED_CASES**（見上 long-run-monitor）。
# 保留空清單維持向後相容的匯入點；未來若再需要 P2c stub 可沿用此位置。
MONITOR_STUB_CASES: list[EvalCase] = []
