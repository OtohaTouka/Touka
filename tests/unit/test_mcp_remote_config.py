"""``ProtocolServerConfig`` 的 stdio／remote 雙形狀契約（ADR-0192，批次 2 / L1）。

批次 2 讓 registry 同時承載本地 stdio（``command``＋``args``）與 remote streamable
HTTP（``url``＋``headers``）兩種條目。兩者**互斥**：形狀不明的條目寧可整筆拒絕，也
不要在連線時才以 ``AttributeError`` 之類的方式炸開——設定檔是使用者可手改的面，
形狀校驗屬 Pydantic 層的職責（禁令 17「從磁碟讀取的設定必須經 model_validate」）。
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from src.protocols.registry import ProtocolRegistry, ProtocolServerConfig
from src.services.mcp_hub_service import MCPHubService
from src.services.resource_manager import resource_manager


class TestStdioShape:
    def test_command_form_still_valid(self):
        cfg = ProtocolServerConfig(command="npx", args=["-y", "pkg@1.0.0"])
        assert cfg.is_remote is False
        assert cfg.command == "npx"

    def test_args_default_to_empty_for_remote(self):
        cfg = ProtocolServerConfig(url="https://x.example/mcp")
        assert cfg.args == []


class TestRemoteShape:
    def test_url_form_valid(self):
        cfg = ProtocolServerConfig(
            url="https://x.example/mcp",
            headers={"Authorization": "Bearer ${PAT}"},
            env={"PAT": "secret"},
        )
        assert cfg.is_remote is True
        assert cfg.command is None

    def test_insecure_remote_url_rejected_at_parse_time(self):
        """URL 安全閘在 model 層就執法，壞條目不會活到連線階段。"""
        with pytest.raises(ValidationError, match="https"):
            ProtocolServerConfig(url="http://evil.example/mcp")


class TestMutualExclusion:
    def test_neither_command_nor_url_rejected(self):
        with pytest.raises(ValidationError):
            ProtocolServerConfig(args=[])

    def test_both_command_and_url_rejected(self):
        """同時給兩種形狀＝意圖不明；靜默挑一個是最糟的選擇。"""
        with pytest.raises(ValidationError):
            ProtocolServerConfig(command="npx", args=[], url="https://x.example/mcp")


class TestHubInstallWritesRemoteShape:
    @pytest.mark.asyncio
    async def test_install_remote_entry_lands_url_and_header_template(self, tmp_path):
        """remote 條目安裝後落盤為 url＋header 模板，憑證實值進 env。

        釘死「憑證不進 headers」這條設計——headers 若直接存 token，就繞過了
        ``_preserve_masked_env`` 的遮罩回填保護（ADR-0192）。
        """
        hub_path = tmp_path / "mcp_hub.json"
        config_path = tmp_path / "server_config.json"
        resource_manager.write_json(
            str(hub_path),
            {"hub": [{
                "id": "registry-acme/hosted",
                "name": "Acme Hosted",
                "url": "https://acme.example/mcp",
                "headers": {"Authorization": "${MCP_AUTHORIZATION}"},
                "env_requirements": ["MCP_AUTHORIZATION"],
                "source": "registry",
            }]},
        )
        svc = MCPHubService(hub_path=str(hub_path), config_path=str(config_path))

        result = await svc.install(
            "registry-acme/hosted", {"MCP_AUTHORIZATION": "Bearer secret-token"}
        )
        assert result["status"] == "success"

        written = resource_manager.read_json(str(config_path))["mcpServers"]["acme-hosted"]
        assert written["url"] == "https://acme.example/mcp"
        assert written["headers"] == {"Authorization": "${MCP_AUTHORIZATION}"}
        assert written["env"]["MCP_AUTHORIZATION"] == "Bearer secret-token"
        # 憑證只存在 env，headers 僅為模板。
        assert "secret-token" not in str(written["headers"])
        # 落盤形狀必須能被 runtime 模型接受（形狀契約不脫鉤）。
        assert ProtocolServerConfig(**written).is_remote is True


class TestEnabledGateAppliesToRemote:
    def test_disabled_remote_entry_excluded_from_registry(self, tmp_path):
        """ADR-0188 的 enabled 咽喉對 remote 條目一視同仁。"""
        config = tmp_path / "server_config.json"
        config.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "on": {"url": "https://a.example/mcp"},
                        "off": {"url": "https://b.example/mcp", "enabled": False},
                    }
                }
            ),
            encoding="utf-8",
        )
        registry = ProtocolRegistry(str(config))
        assert set(registry.servers) == {"on"}
