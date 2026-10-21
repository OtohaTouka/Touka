"""執行期自訂模型路由。

路由端點與憑證是不可拆分的安全單位；只存在記憶體，不進模型 descriptor、API 或持久化設定。
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class CustomModelRoute:
    """由單一受管理連線產生的不可變模型路由。"""

    connection_id: str
    endpoint: str = field(repr=False)
    api_key: str = field(repr=False)
    provider: str


class ModelRouteSnapshotState(StrEnum):
    """自訂模型路由快照的可用狀態。"""

    INITIALIZING = "initializing"
    READY = "ready"
    DEGRADED = "degraded"


class CustomRouteDisposition(StrEnum):
    """Dispatcher 對模型來源的三態判定。"""

    ACTIVE_CUSTOM = "active_custom"
    KNOWN_CUSTOM_INACTIVE = "known_custom_inactive"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ModelRouteDiagnostic:
    """不含原始識別字或連線內容的路由診斷。"""

    connection_handle: str
    code: str


@dataclass(frozen=True, slots=True)
class ModelRouteSnapshot:
    """單次發布的不可變自訂模型路由 SSOT。"""

    generation: int
    config_fingerprint: str
    active_routes: Mapping[str, CustomModelRoute]
    known_custom_model_ids: frozenset[str]
    diagnostics: tuple[ModelRouteDiagnostic, ...]
    state: ModelRouteSnapshotState

    @classmethod
    def create(
        cls,
        *,
        generation: int,
        config_fingerprint: str,
        active_routes: Mapping[str, CustomModelRoute] | None = None,
        known_custom_model_ids: frozenset[str] | None = None,
        diagnostics: tuple[ModelRouteDiagnostic, ...] = (),
        state: ModelRouteSnapshotState = ModelRouteSnapshotState.INITIALIZING,
    ) -> "ModelRouteSnapshot":
        """複製可變輸入並封裝為不可變 mapping，避免發布後被原地修改。"""
        return cls(
            generation=generation,
            config_fingerprint=config_fingerprint,
            active_routes=MappingProxyType(dict(active_routes or {})),
            known_custom_model_ids=frozenset(known_custom_model_ids or ()),
            diagnostics=tuple(diagnostics),
            state=state,
        )


@dataclass(frozen=True, slots=True)
class CustomRouteResolution:
    """一次 snapshot read 產生的原子路由判定；``original_name`` 是自訂連線的原名（ADR-0250 A5）。"""

    disposition: CustomRouteDisposition
    route: CustomModelRoute | None = None
    original_name: str | None = None
