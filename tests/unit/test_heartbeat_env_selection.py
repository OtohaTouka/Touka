"""Heartbeat environment and import isolation, executable before app collection."""

import ast
import asyncio
import importlib
import importlib.util
import json
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
CONFIG_SOURCE = ROOT / "ToukaHeartbeat" / "config.py"


class TestHeartbeatEnvironmentSelection(unittest.TestCase):
    """Load the exact config source from an entirely owned fixture tree."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="heartbeat-env-test-")
        self.addCleanup(self.temporary.cleanup)
        self.owned = Path(self.temporary.name)
        self.heartbeat = self.owned / "ToukaHeartbeat"
        self.heartbeat.mkdir()
        self.config = self.heartbeat / "config.py"
        self.config.write_bytes(CONFIG_SOURCE.read_bytes())
        self.selected = self.owned / "selected.env"
        (self.owned / ".env").write_text("HEARTBEAT_TOKEN=owned-fallback\n", encoding="utf-8")
        (self.heartbeat / ".env").write_text("HEARTBEAT_TOKEN=owned-standalone\n", encoding="utf-8")

    def load(self, explicit: str, *, integrated: str = "", cwd: Path | None = None):
        specification = importlib.util.spec_from_file_location(
            "owned_heartbeat_config", self.config
        )
        assert specification is not None and specification.loader is not None
        module = importlib.util.module_from_spec(specification)
        environment = {
            "TOUKA_ENV_FILE": explicit,
            "TOUKA_INTEGRATED": integrated,
            "HEARTBEAT_TOKEN": "",
        }
        with (
            patch.dict(os.environ, environment),  # noqa: ENV_SSOT_VIOLATION
            patch.object(Path, "cwd", return_value=cwd or self.owned),
            patch.dict(sys.modules, {specification.name: module}),
        ):
            # Remove only this key so the actual dotenv source can supply it.
            os.environ.pop("HEARTBEAT_TOKEN")  # noqa: ENV_SSOT_VIOLATION
            specification.loader.exec_module(module)
            return module.resolve_heartbeat_env_file_path(), module.settings.HEARTBEAT_TOKEN

    def test_present_explicit_wins(self) -> None:
        self.selected.write_text("HEARTBEAT_TOKEN=owned-selected\n", encoding="utf-8")
        selected, token = self.load(str(self.selected))
        self.assertEqual(selected, self.selected)
        self.assertEqual(token, "owned-selected")

    def test_missing_explicit_does_not_fall_back(self) -> None:
        selected, token = self.load(str(self.selected))
        self.assertEqual(selected, self.selected)
        self.assertEqual(token, "")

    def test_explicit_wins_over_integrated_switch(self) -> None:
        selected, token = self.load(str(self.selected), integrated="true")
        self.assertEqual(selected, self.selected)
        self.assertEqual(token, "")

    def test_explicit_does_not_probe_default_env(self) -> None:
        def denied_exists(_path: Path) -> bool:
            raise AssertionError("implicit_env_probe_forbidden")

        with patch.object(Path, "exists", denied_exists):
            selected, _token = self.load(str(self.selected))
        self.assertEqual(selected, self.selected)

    def test_no_explicit_keeps_integrated_source(self) -> None:
        selected, token = self.load("")
        self.assertEqual(selected, self.owned / ".env")
        self.assertEqual(token, "owned-fallback")

    def test_no_explicit_keeps_standalone_source(self) -> None:
        selected, token = self.load("", cwd=self.heartbeat)
        self.assertEqual(selected, self.heartbeat / ".env")
        self.assertEqual(token, "owned-standalone")


def run_import_audit_child(owned: Path) -> None:
    """Observe actual import and first log event without opening private inputs."""
    owned.mkdir(exist_ok=True)
    selected = owned / "selected.env"
    selected.write_text("# owned import audit\n", encoding="utf-8")
    os.environ.update(  # noqa: ENV_SSOT_VIOLATION
        TOUKA_ENV_FILE=str(selected), HEARTBEAT_CONFIG_FILE=str(owned / "config.json")
    )
    logging.getLogger().addHandler(logging.NullHandler())
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "ToukaHeartbeat"))
    forbidden = {
        os.path.normcase(str(ROOT / ".env")): "workspace-env",
        os.path.normcase(str(ROOT / "ToukaHeartbeat" / ".env")): "heartbeat-env",
        os.path.normcase(str(ROOT / "ToukaHeartbeat" / "config.json")): "heartbeat-config",
        os.path.normcase(str(ROOT / "ToukaHeartbeat" / "heartbeat.log")): "heartbeat-log",
    }

    def audit(event: str, arguments: tuple) -> None:
        if event == "open" and isinstance(arguments[0], (str, bytes, os.PathLike)):
            normalized = os.path.normcase(os.path.abspath(os.fsdecode(arguments[0])))
            category = forbidden.get(normalized)
            if category:
                raise AssertionError(f"blocked_non_owned_import_io:{category}")

    sys.addaudithook(audit)
    module = importlib.import_module("ToukaHeartbeat.awaken")
    module.logger.warning("synthetic first-event import audit")
    assert owned / "config.json" == module.CONFIG_FILE
    assert module.settings.model_config.get("env_file_encoding") == "utf-8"
    print("IMPORT_AND_FIRST_LOG_EVENT_OWNED_BOUNDARY_PASS")  # noqa: T201


def run_restart_audit_child(owned: Path) -> None:
    """Fresh actual imports consume a previously persisted owned setting."""
    specification = json.loads((owned / "probe.json").read_text(encoding="utf-8"))
    selected = Path(specification["env_file"])
    os.environ.update(  # noqa: ENV_SSOT_VIOLATION
        TOUKA_ENV_FILE=str(selected),
        HEARTBEAT_CONFIG_FILE=str(owned / "heartbeat.json"),
        TOUKA_CONFIG_DIR=str(owned / "config"),
        SANCTUARY_DATA_PATH=str(owned / "data"),
        MEMORY_APP_DATA_DIR=str(owned / "memory"),
        TOUKA_BOOTSTRAP_ENABLED="false",
        DISPATCH_AUTOSPAWN="false",
        ENABLE_DYNAMIC_MODELS="false",
        TOUKA_EVAL_JUDGE_ENABLED="false",
        TOUKA_AUTH_SECRET="synthetic-restart-signing-key-32bytes",
    )
    os.environ.pop("HEARTBEAT_TOKEN", None)  # noqa: ENV_SSOT_VIOLATION
    logging.getLogger().addHandler(logging.NullHandler())
    for directory in (ROOT, ROOT / "ToukaProxy", ROOT / "ToukaMemory" / "src"):
        sys.path.insert(0, str(directory))
    forbidden_files = {
        ROOT / ".env",
        ROOT / "ToukaProxy" / ".env",
        ROOT / "ToukaHeartbeat" / ".env",
        ROOT / "ToukaHeartbeat" / "config.json",
        ROOT / "ToukaHeartbeat" / "heartbeat.log",
    }
    forbidden_trees = [ROOT / "data", ROOT / "soul", ROOT / "ToukaMemory" / "data"]

    def audit(event: str, arguments: tuple) -> None:
        if event != "open" or not isinstance(arguments[0], (str, bytes, os.PathLike)):
            return
        path = Path(os.path.abspath(os.fsdecode(arguments[0])))
        if path in forbidden_files or any(path.is_relative_to(tree) for tree in forbidden_trees):
            raise AssertionError("blocked_non_owned_restart_io")
        if path.is_relative_to(ROOT / "ToukaProxy" / "config") and not path.name.endswith(
            ".example"
        ):
            raise AssertionError("blocked_private_config_restart_io")

    sys.addaudithook(audit)
    policy = importlib.import_module("tests.live_api_policy")
    for name in policy.PROVIDER_CREDENTIAL_ENV_VARS:
        os.environ[name] = ""  # noqa: ENV_SSOT_VIOLATION
    bootstrap = importlib.import_module("src.core.bootstrap")
    bootstrap.hydrate_config_examples(ROOT / "ToukaProxy" / "config", owned / "config")
    proxy_config = importlib.import_module("src.config")
    proxy_router = importlib.import_module("src.routers.heartbeat")
    heartbeat = importlib.import_module("ToukaHeartbeat.awaken")
    requests_module = importlib.import_module("starlette.requests")
    httpx_module = importlib.import_module("httpx")
    expected = specification["expected"]
    assert proxy_config.settings.HEARTBEAT_TOKEN == proxy_config.HEARTBEAT_TOKEN == expected
    assert proxy_router.HEARTBEAT_TOKEN == heartbeat.settings.HEARTBEAT_TOKEN == expected
    assert heartbeat.proxy_client.token_provider is heartbeat.token_provider
    result = heartbeat.token_provider.resolve(require_shared=True)
    assert result.value == expected and result.shared_ready
    request = requests_module.Request({"type": "http", "headers": []})
    verify = proxy_router.verify_heartbeat_token
    model = proxy_router.HeartbeatAwakenRequest
    assert verify(request, model(token=expected, agent_id="DebugAgent")) == expected
    heartbeat.verify_schedule_token(
        requests_module.Request(
            {
                "type": "http",
                "headers": [(b"authorization", f"Bearer {expected}".encode())],
            }
        )
    )
    old = specification.get("old")
    if old:
        assert not result.accepts(old)
        try:
            verify(request, model(token=old, agent_id="DebugAgent"))
        except proxy_router.ToukaException:
            pass
        else:
            raise AssertionError("old_shared_credential_accepted")
    sent = []
    original_client = httpx_module.AsyncClient

    def receive(outbound):
        body = json.loads(outbound.content)
        assert verify(request, model(**body)) == expected
        sent.append(body)
        return httpx_module.Response(200)

    async def exercise() -> None:
        assert await heartbeat.proxy_client.trigger_heartbeat("DebugAgent", 2, 4) == 200
        tasks = []

        def consume(coroutine):
            tasks.append(True)
            coroutine.close()

        with patch.object(heartbeat, "asyncio", SimpleNamespace(create_task=consume)):
            response = await heartbeat.handle_manual_trigger(
                heartbeat.TriggerPayload(
                    token=expected,
                    agent_id="DebugAgent",
                )
            )
        assert response["status"] == "dispatched" and tasks == [True]

    def client_factory(**kwargs):
        assert kwargs["follow_redirects"] is False
        return original_client(transport=httpx_module.MockTransport(receive), **kwargs)

    with patch.object(httpx_module, "AsyncClient", client_factory):
        asyncio.run(exercise())
    assert len(sent) == 1 and sent[0]["token"] == expected
    heartbeat.logger.warning("synthetic first restart log event")
    print("FRESH_RESTART_IMPORT_AND_TRANSPORT_PASS")  # noqa: T201


class TestHeartbeatImportIsolation(unittest.TestCase):
    def test_pytest_heartbeat_path_is_owned_before_product_imports(self) -> None:
        source = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        first_product = min(
            node.lineno
            for node in tree.body
            if isinstance(node, ast.ImportFrom)
            and (node.module or "").startswith(("src", "memory_server"))
        )
        assignments = [
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                ast.unparse(target) == "os.environ['HEARTBEAT_CONFIG_FILE']"
                for target in node.targets
            )
        ]
        self.assertEqual(len(assignments), 1)
        self.assertLess(assignments[0].lineno, first_product)
        self.assertIn("_SESSION_TMP_ROOT", ast.unparse(assignments[0].value))

    def test_pytest_installs_fallback_logging_without_replacing_capture(self) -> None:
        source = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        conditional = [
            node
            for node in tree.body
            if isinstance(node, ast.If)
            and ast.unparse(node.test) == "not logging.getLogger().handlers"
        ]
        self.assertEqual(len(conditional), 1)
        self.assertIn(
            "logging.getLogger().addHandler(logging.NullHandler())", ast.unparse(conditional[0])
        )
        self.assertLess(
            conditional[0].lineno,
            source.splitlines().index(
                "from src.core.bootstrap import hydrate_config_examples  # noqa: E402"
            )
            + 1,
        )

    def test_actual_import_and_first_log_event_remain_isolated(self) -> None:
        with tempfile.TemporaryDirectory(prefix="heartbeat-import-test-") as temporary:
            result = subprocess.run(  # noqa: S603
                [sys.executable, "-B", str(Path(__file__)), "--audit-child", temporary],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("IMPORT_AND_FIRST_LOG_EVENT_OWNED_BOUNDARY_PASS", result.stdout)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--restart-child":
        run_restart_audit_child(Path(sys.argv[2]))
    elif len(sys.argv) == 3 and sys.argv[1] == "--audit-child":
        run_import_audit_child(Path(sys.argv[2]))
    else:
        unittest.main()
