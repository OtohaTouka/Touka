# ruff: noqa: D102, S105, TC003
"""P8-A non-secret OAuth profile and exact scope policy。"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from filelock import AsyncFileLock  # noqa: FILELOCK_ASYNC_SSOT
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from src.services.resource_manager import resource_manager

OAuthOperation = Literal["tools/call", "resources/read", "prompts/get"]
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
_SCOPE_TOKEN_RE = re.compile(r"^[\x21\x23-\x5B\x5D-\x7E]+$")


class OAuthScopeError(ValueError):
    """Authorization server returned an invalid or expanded scope set。"""


class OAuthProfileConflictError(ValueError):
    """Profile generation is stale or an initial profile already exists。"""


class OAuthProfileDocumentError(ValueError):
    """Profile document is corrupt or violates its strict schema。"""


def _validate_https_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError()
    return value


class OAuthScopeRule(BaseModel):
    """One exact or whole-operation wildcard scope rule。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: OAuthOperation
    selector: str = Field(min_length=1, max_length=4096)
    scopes: frozenset[str] = Field(min_length=1)

    @field_validator("selector")
    @classmethod
    def validate_selector(cls, value: str) -> str:
        if "*" in value and value != "*":
            raise ValueError()
        return value

    @field_validator("scopes")
    @classmethod
    def validate_scopes(cls, value: frozenset[str]) -> frozenset[str]:
        if not value or any(not _SCOPE_TOKEN_RE.fullmatch(scope) for scope in value):
            raise ValueError()
        return value


class OAuthPrivateNetworkException(BaseModel):
    """Exact installation/origin exception for explicitly approved CIDRs。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    installation_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
    origin: str
    cidrs: tuple[str, ...] = Field(min_length=1)

    @field_validator("origin")
    @classmethod
    def validate_origin(cls, value: str) -> str:
        _validate_https_url(value)
        parsed = urlsplit(value)
        if parsed.path not in {"", "/"} or parsed.query:
            raise ValueError()
        return value.removesuffix("/")

    @field_validator("cidrs")
    @classmethod
    def validate_cidrs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        try:
            networks = tuple(ipaddress.ip_network(item, strict=True) for item in value)
        except ValueError as exc:
            raise ValueError() from exc
        canonical = tuple(str(network) for network in networks)
        if len(set(canonical)) != len(canonical) or any(
            network.prefixlen == 0 for network in networks
        ):
            raise ValueError()
        return canonical


class OAuthProfile(BaseModel):
    """Strict, non-secret OAuth profile document。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    generation: int = Field(ge=1)
    enabled: bool = False
    callback_binding: Literal["form_post", "query"] = "form_post"
    trusted_public_origin: str
    trusted_issuers: tuple[str, ...] = Field(min_length=1)
    client_id: str | None = Field(default=None, min_length=1, max_length=2048)
    scope_rules: tuple[OAuthScopeRule, ...] = ()
    dcr_enabled: bool = False
    dcr_trusted_issuers: tuple[str, ...] = ()
    token_endpoint_auth_method: Literal["none", "client_secret_post"] = "none"
    private_network_exceptions: tuple[OAuthPrivateNetworkException, ...] = ()

    @field_validator("trusted_public_origin")
    @classmethod
    def validate_public_origin(cls, value: str) -> str:
        _validate_https_url(value)
        parsed = urlsplit(value)
        if parsed.path not in {"", "/"} or parsed.query:
            raise ValueError()
        return value.removesuffix("/")

    @field_validator("trusted_issuers")
    @classmethod
    def validate_issuers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError()
        return tuple(_validate_https_url(issuer) for issuer in value)

    @model_validator(mode="after")
    def validate_unique_scope_rules(self) -> OAuthProfile:
        keys = [(rule.operation, rule.selector) for rule in self.scope_rules]
        if len(set(keys)) != len(keys):
            raise ValueError()
        if self.client_id is None and not self.dcr_enabled:
            raise ValueError()
        if self.dcr_enabled and (
            not self.dcr_trusted_issuers
            or len(set(self.dcr_trusted_issuers)) != len(self.dcr_trusted_issuers)
            or not set(self.dcr_trusted_issuers).issubset(self.trusted_issuers)
        ):
            raise ValueError()
        exception_keys = {
            (exception.installation_id, exception.origin)
            for exception in self.private_network_exceptions
        }
        if len(exception_keys) != len(self.private_network_exceptions):
            raise ValueError()
        return self

    @field_validator("dcr_trusted_issuers")
    @classmethod
    def validate_dcr_issuers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_validate_https_url(issuer) for issuer in value)

    def required_scopes(
        self,
        operation: OAuthOperation,
        selector: str,
    ) -> frozenset[str] | None:
        """Resolve exact first, then a whole-operation wildcard; otherwise deny。"""
        wildcard: frozenset[str] | None = None
        for rule in self.scope_rules:
            if rule.operation != operation:
                continue
            if rule.selector == selector:
                return rule.scopes
            if rule.selector == "*":
                wildcard = rule.scopes
        return wildcard

    def allowed_private_cidrs(
        self,
        installation_id: str,
        url: str,
    ) -> tuple[IPNetwork, ...]:
        """Resolve only an exact installation plus HTTPS origin exception。"""
        parsed = urlsplit(_validate_https_url(url))
        origin = f"{parsed.scheme}://{parsed.netloc}"
        for exception in self.private_network_exceptions:
            if exception.installation_id == installation_id and exception.origin == origin:
                return tuple(ipaddress.ip_network(cidr, strict=True) for cidr in exception.cidrs)
        return ()


class OAuthProfileDocument(BaseModel):
    """Versioned, non-secret profile repository document。"""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    profiles: dict[str, OAuthProfile] = Field(default_factory=dict)

    @model_validator(mode="after")
    def keys_match_profile_ids(self) -> OAuthProfileDocument:
        if any(key != profile.profile_id for key, profile in self.profiles.items()):
            raise OAuthProfileDocumentError()
        return self


def oauth_profile_fingerprint(profile: OAuthProfile) -> str:
    """Return the canonical SHA-256 fingerprint used by durable mutation fences。"""
    canonical = json.dumps(
        profile.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class OAuthProfileRepository:
    """Atomic ResourceManager JSON repository with a cross-process mutation lock。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = AsyncFileLock(str(self._path) + ".lock", timeout=30)

    @asynccontextmanager
    async def profile_guard(self, profile_id: str) -> AsyncIterator[None]:
        """Serialize profile mutation and credential egress across workers。"""
        digest = hashlib.sha256(profile_id.encode("utf-8")).hexdigest()
        lock = AsyncFileLock(
            f"{self._path}.profile-{digest}.lock",
            timeout=30,
        )
        async with lock:
            yield

    async def _load(self) -> OAuthProfileDocument:
        exists = await asyncio.to_thread(resource_manager.exists, self._path)
        raw = await asyncio.to_thread(resource_manager.read_json, self._path)
        if raw is None:
            if exists:
                raise OAuthProfileDocumentError()
            return OAuthProfileDocument()
        try:
            return OAuthProfileDocument.model_validate(raw)
        except (ValueError, TypeError) as exc:
            raise OAuthProfileDocumentError() from exc

    async def _write(self, document: OAuthProfileDocument) -> None:
        await asyncio.to_thread(
            resource_manager.write_json,
            self._path,
            document.model_dump(mode="json"),
            "utf-8",
            2,
        )

    async def list_profiles(self) -> list[OAuthProfile]:
        """Return a deterministic snapshot without exposing mutable cache state。"""
        document = await self._load()
        return [document.profiles[key] for key in sorted(document.profiles)]

    async def get(self, profile_id: str) -> OAuthProfile | None:
        return (await self._load()).profiles.get(profile_id)

    async def put(self, profile: OAuthProfile) -> OAuthProfile:
        """Create at generation 1 or replace using exact generation+1 CAS。"""
        async with self._lock:
            document = await self._load()
            existing = document.profiles.get(profile.profile_id)
            if (
                existing is None
                and profile.generation != 1
                or existing is not None
                and profile.generation != existing.generation + 1
            ):
                raise OAuthProfileConflictError()
            profiles = dict(document.profiles)
            profiles[profile.profile_id] = profile
            await self._write(document.model_copy(update={"profiles": profiles}))
        return profile

    async def delete(self, profile_id: str) -> None:
        async with self._lock:
            document = await self._load()
            if profile_id not in document.profiles:
                raise OAuthProfileConflictError()
            profiles = dict(document.profiles)
            profiles.pop(profile_id)
            await self._write(document.model_copy(update={"profiles": profiles}))


def _parse_scope_set(value: str) -> frozenset[str]:
    if not value or value != value.strip() or "  " in value:
        raise OAuthScopeError()
    tokens = value.split(" ")
    if len(set(tokens)) != len(tokens) or any(
        not _SCOPE_TOKEN_RE.fullmatch(token) for token in tokens
    ):
        raise OAuthScopeError()
    return frozenset(tokens)


def validate_returned_scopes(
    returned: str | None,
    existing_or_requested: frozenset[str],
    *,
    refresh: bool,
) -> frozenset[str]:
    """Apply OAuth initial/refresh omission and subset-only rules。"""
    del refresh
    if returned is None:
        return existing_or_requested
    parsed = _parse_scope_set(returned)
    if not parsed.issubset(existing_or_requested):
        raise OAuthScopeError()
    return parsed
