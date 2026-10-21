"""ADR-0069 Phase B（EVAL）單元測試。

涵蓋：模型預設值、eval.md round-trip、skill/mcp 評測任務生成（決定性、上限）、
EvalRunner 持久化（``config/evals/<kind>/<id>.eval.md``）、引擎後置非阻斷整合、
旗標（run_eval/eval_scoring）、judge DIP 接口（NoopJudge / 注入），以及安全性
（MCP 唯讀枚舉、不碰既有產物、無時間戳故決定性）。

沙箱由 ``tests/conftest.py`` 提供（``TOUKA_CONFIG_DIR`` → tmp）；所有產物寫入沙箱、
使用唯一臨時 id，零接觸真實 agent 資料。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from src.config import get_config_dir
from src.services.creation import creation_engine
from src.services.creation.eval_formatter import parse_eval_md, serialize_eval_md
from src.services.creation.evaluation import EvalRunner, NoopJudge, eval_runner
from src.services.creation.kinds import launch_context
from src.services.creation.mcp_kind import McpCreationKind
from src.services.creation.models import (
    CommitResult,
    EvalReport,
    EvalTask,
    McpSpec,
    SkillSpec,
    SmokeReport,
)
from src.services.creation.skill_kind import SkillCreationKind
from src.services.creation.smoke import ToolInfo
from src.services.resource_manager import resource_manager
from src.services.skill_service import skill_service

_PATCH_LIST_TOOLS = "src.services.creation.mcp_kind.list_mcp_tools"
_PATCH_MCP_SMOKE = "src.services.creation.mcp_kind.smoke_mcp_server"
_PATCH_SERIALIZE = "src.services.creation.evaluation.serialize_eval_md"
_FAKE_TOOLS = [ToolInfo(name="search"), ToolInfo(name="read")]


def _skill_eval_path(skill_id: str):
    return get_config_dir() / "evals" / "skill" / f"{skill_id}.eval.md"


def _mcp_eval_path(name: str):
    return get_config_dir() / "evals" / "mcp" / f"{name}.eval.md"


def _skill_md_path(skill_id: str):
    return get_config_dir() / "skills" / skill_id / "SKILL.md"


def _server_config_path():
    return get_config_dir() / "server_config.json"


# --- 模型 + 格式器（純） ---


def test_eval_task_and_report_defaults():
    t = EvalTask(id="t1", title="title")
    assert t.description == "" and t.expectation == "" and t.probe == ""
    r = EvalReport(ok=True)
    assert r.tasks == [] and r.scored is False and r.score is None
    assert r.artifact is None and r.warnings == []


def test_eval_md_roundtrip():
    report = EvalReport(
        ok=True,
        kind="skill",
        id="demo-skill",
        detail="2 題",
        tasks=[
            EvalTask(
                id="demo-skill-tool-1",
                title="用工具 read",
                probe="read",
                expectation="會以唯讀方式選用 read",
                description="中文描述不可失真",
            ),
            EvalTask(id="demo-skill-desc", title="觸發", expectation="符合意圖", description="d"),
        ],
        warnings=["w1"],
    )
    parsed = parse_eval_md(serialize_eval_md(report))
    assert parsed == report


def test_eval_md_is_deterministic():
    spec = SkillSpec(id="eval-determ", description="d", required_tools=["a", "b"])
    kind = SkillCreationKind()
    r1 = EvalReport(
        ok=True, kind="skill", id="eval-determ",
        tasks=kind.generate_eval_tasks(spec, list(spec.required_tools)),
    )
    r2 = EvalReport(
        ok=True, kind="skill", id="eval-determ",
        tasks=kind.generate_eval_tasks(spec, list(spec.required_tools)),
    )
    assert serialize_eval_md(r1) == serialize_eval_md(r2)


# --- 生成（純、決定性） ---


def test_skill_generates_n_tasks():
    spec = SkillSpec(id="eval-skill-a", description="d", required_tools=["read", "search"])
    tasks = SkillCreationKind().generate_eval_tasks(spec, list(spec.required_tools))
    assert len(tasks) == 3  # 2 工具 + 1 描述觸發題
    assert tasks[0].probe == "read"
    assert tasks[-1].id == "eval-skill-a-desc"


def test_mcp_one_task_per_tool():
    spec = McpSpec(name="evalsrv", command="uvx", args=[])
    tasks = McpCreationKind().generate_eval_tasks(spec, _FAKE_TOOLS)
    assert len(tasks) == 2
    assert [t.probe for t in tasks] == ["search", "read"]


def test_mcp_caps_tasks_at_max():
    spec = McpSpec(name="bigsrv", command="uvx", args=[])
    tools = [ToolInfo(name=f"t{i}") for i in range(30)]
    assert len(McpCreationKind().generate_eval_tasks(spec, tools)) == 20


# --- EvalRunner 持久化 ---


async def test_eval_runner_writes_skill_eval_md():
    spec = SkillSpec(id="eval-skill-write", description="d", required_tools=["read"])
    commit = CommitResult(id="eval-skill-write")
    report = await EvalRunner().run("skill", SkillCreationKind(), spec, commit)
    assert report.ok is True
    path = _skill_eval_path("eval-skill-write")
    assert resource_manager.exists(path)
    assert report.artifact == str(path)
    parsed = parse_eval_md(resource_manager.read_file_text(path))
    assert parsed.id == "eval-skill-write"
    assert len(parsed.tasks) == 2  # 1 工具 + 1 描述題


async def test_eval_runner_writes_mcp_eval_md():
    spec = McpSpec(name="evalwrite", command="uvx", args=[])
    commit = CommitResult(id="evalwrite")
    with patch(_PATCH_LIST_TOOLS, new=AsyncMock(return_value=_FAKE_TOOLS)):
        report = await EvalRunner().run(
            "mcp", McpCreationKind(), spec, commit, context=await launch_context("creation"),
        )
    assert report.ok is True
    path = _mcp_eval_path("evalwrite")
    assert resource_manager.exists(path)
    parsed = parse_eval_md(resource_manager.read_file_text(path))
    assert len(parsed.tasks) == 2
    assert parsed.tasks[0].probe == "search"


# --- 引擎後置整合 ---


async def test_eval_runs_post_commit_and_writes():
    res = await creation_engine.create(
        "skill", SkillSpec(id="eval-post-skill", description="d", system_prompt="b")
    )
    assert res.committed is True
    assert res.evaluation is not None
    assert res.evaluation.ok is True
    assert resource_manager.exists(_skill_eval_path("eval-post-skill"))


async def test_eval_post_commit_mcp():
    resource_manager.write_json(_server_config_path(), {"mcpServers": {}})
    with (
        patch(_PATCH_MCP_SMOKE, new=AsyncMock(return_value=SmokeReport(ok=True, tool_count=2))),
        patch(_PATCH_LIST_TOOLS, new=AsyncMock(return_value=_FAKE_TOOLS)),
    ):
        res = await creation_engine.create(
            "mcp", McpSpec(name="evalpost", command="uvx", args=["x"]),
            context=await launch_context("creation"),
        )
    assert res.committed is True
    assert res.evaluation is not None and res.evaluation.ok is True
    assert len(res.evaluation.tasks) == 2
    assert resource_manager.exists(_mcp_eval_path("evalpost"))


async def test_eval_is_non_blocking_on_generation_error():
    # eval 在 commit 之後；序列化爆掉 → committed 仍 True、evaluation.ok False、產物仍在。
    with patch(_PATCH_SERIALIZE, side_effect=RuntimeError("boom")):
        res = await creation_engine.create(
            "skill", SkillSpec(id="eval-nonblock", description="d", system_prompt="b")
        )
    assert res.committed is True
    assert res.evaluation is not None
    assert res.evaluation.ok is False
    assert skill_service.get_skill("eval-nonblock") is not None
    assert not resource_manager.exists(_skill_eval_path("eval-nonblock"))


# --- 旗標 ---


async def test_run_eval_false_skips():
    res = await creation_engine.create(
        "skill",
        SkillSpec(id="eval-skip", description="d", system_prompt="b"),
        run_eval=False,
    )
    assert res.committed is True
    assert res.evaluation is None
    assert not resource_manager.exists(_skill_eval_path("eval-skip"))


async def test_eval_scoring_defaults_off():
    res = await creation_engine.create(
        "skill", SkillSpec(id="eval-noscore", description="d", system_prompt="b")
    )
    assert res.evaluation is not None
    assert res.evaluation.scored is False
    assert res.evaluation.score is None


async def test_eval_scoring_true_without_real_judge_is_noop():
    # 模組單例 eval_runner 無 judge → 即使 eval_scoring=True 也不計分（零付費呼叫）。
    res = await creation_engine.create(
        "skill",
        SkillSpec(id="eval-noop-judge", description="d", system_prompt="b"),
        eval_scoring=True,
    )
    assert res.evaluation is not None
    assert res.evaluation.scored is False
    assert res.evaluation.score is None


# --- Judge DIP 接口 ---


async def test_injected_judge_scores_when_enabled():
    class _FakeJudge:
        async def score(self, tasks, artifact_md, identity=None):
            return 0.75

    spec = SkillSpec(id="eval-judge", description="d")
    report = await EvalRunner(judge=_FakeJudge()).run(
        "skill", SkillCreationKind(), spec, CommitResult(id="eval-judge"), scoring=True
    )
    assert report.scored is True
    assert report.score == 0.75


async def test_noop_judge_does_not_score():
    spec = SkillSpec(id="eval-noopj", description="d")
    report = await EvalRunner(judge=NoopJudge()).run(
        "skill", SkillCreationKind(), spec, CommitResult(id="eval-noopj"), scoring=True
    )
    assert report.scored is False
    assert report.score is None


# --- 安全 ---


async def test_mcp_eval_uses_readonly_list_only():
    # EVAL 的 MCP 探測只走唯讀的 list_mcp_tools（tools/list），絕不執行工具。
    spec = McpSpec(name="evalro", command="uvx", args=[])
    mock = AsyncMock(return_value=_FAKE_TOOLS)
    with patch(_PATCH_LIST_TOOLS, new=mock):
        probes = await McpCreationKind().probe_eval_inputs(spec, await launch_context("creation"))
    mock.assert_awaited_once()
    assert probes == _FAKE_TOOLS


async def test_eval_does_not_touch_existing_artifact():
    await creation_engine.create(
        "skill",
        SkillSpec(id="eval-keep", name="Keep", description="d", system_prompt="orig"),
    )
    before_md = resource_manager.read_file_text(_skill_md_path("eval-keep"))
    before_eval = resource_manager.read_file_text(_skill_eval_path("eval-keep"))
    await creation_engine.create(
        "skill", SkillSpec(id="eval-other", description="d2", system_prompt="b2")
    )
    after_md = resource_manager.read_file_text(_skill_md_path("eval-keep"))
    after_eval = resource_manager.read_file_text(_skill_eval_path("eval-keep"))
    assert before_md == after_md
    assert before_eval == after_eval


# --- Phase D：set_judge 注入（加法接口）回歸 ---


async def test_set_judge_injects_real_scoring_then_restores():
    """注入真 judge → scoring=True 計分；scoring=False 不呼叫；最後復原（單例隔離）。"""

    class _FakeJudge:
        def __init__(self) -> None:
            self.calls = 0

        async def score(self, tasks, artifact_md, identity=None) -> float:
            self.calls += 1
            return 0.9

    fake = _FakeJudge()
    eval_runner.set_judge(fake)
    try:
        r1 = await eval_runner.run(
            "skill",
            SkillCreationKind(),
            SkillSpec(id="eval-judge-on", description="d"),
            CommitResult(id="eval-judge-on"),
            scoring=True,
        )
        assert r1.scored is True and r1.score == 0.9
        assert fake.calls == 1
        r2 = await eval_runner.run(
            "skill",
            SkillCreationKind(),
            SkillSpec(id="eval-judge-off", description="d"),
            CommitResult(id="eval-judge-off"),
            scoring=False,
        )
        assert r2.scored is False and r2.score is None
        assert fake.calls == 1  # scoring=False → judge 未被呼叫
    finally:
        eval_runner.set_judge(None)


async def test_eval_runner_default_singleton_no_scoring():
    """模組單例 eval_runner 預設無 judge → 即使 scoring=True 也不計分（零成本保證）。"""
    r = await eval_runner.run(
        "skill",
        SkillCreationKind(),
        SkillSpec(id="eval-default-noscore", description="d"),
        CommitResult(id="eval-default-noscore"),
        scoring=True,
    )
    assert r.scored is False
    assert r.score is None
