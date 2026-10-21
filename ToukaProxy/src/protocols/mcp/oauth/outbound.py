# ruff: noqa: ANN001, ANN002, ANN003, ANN201, ARG002, D102, S105, S106, S107, TRY301
"""P8-A OAuth HTTPS, DNS and pinned-address egress policy。"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import socket
import ssl
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

import httpcore2 as httpcore
import httpx2 as httpx
from pydantic import SecretStr
from src.config import (
    OAUTH_HTTP_CONNECT_TIMEOUT_SECONDS,
    OAUTH_HTTP_MAX_JSON_DEPTH,
    OAUTH_HTTP_MAX_JSON_ITEMS,
    OAUTH_HTTP_MAX_RESPONSE_BYTES,
    OAUTH_HTTP_POOL_TIMEOUT_SECONDS,
    OAUTH_HTTP_READ_TIMEOUT_SECONDS,
    OAUTH_HTTP_WRITE_TIMEOUT_SECONDS,
)
from src.protocols.mcp.oauth.models import (
    OAuthInvalidGrantError,
    OAuthTokenSet,
)
from src.protocols.mcp.oauth.registration import OAuthClientBinding
from src.protocols.mcp.oauth.service import OAuthAuthorizationMetadata
from src.utils.time_utils import get_current_epoch

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable

    from httpcore2 import AsyncNetworkBackend, AsyncNetworkStream
    from src.protocols.mcp.oauth.models import OAuthPreparedRefreshRequest

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
OAuthPrivateNetworkPolicy = Callable[[str], tuple[IPNetwork, ...]]


class OAuthOutboundPolicyError(ValueError):
    """OAuth egress target failed the pre-credential policy。"""


class OAuthUpstreamError(ValueError):
    """Sanitized upstream failure; response bodies are never attached。"""


class OAuthTransportError(OAuthUpstreamError):
    """Sanitized failure before an OAuth HTTP response was received。"""


class OAuthTokenProtocolError(OAuthUpstreamError):
    """A successful token response violated bounded JSON framing。"""


class OAuthMetadataDiscoveryError(ValueError):
    """Protected-resource or authorization-server metadata failed exact binding。"""


class OAuthResolver(Protocol):
    """Injectable DNS resolver returning the complete address set。"""

    async def resolve(self, host: str, port: int) -> tuple[IPAddress, ...]: ...


class SystemOAuthResolver:
    """Non-blocking wrapper around system getaddrinfo。"""

    async def resolve(self, host: str, port: int) -> tuple[IPAddress, ...]:
        infos = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            port,
            0,
            socket.SOCK_STREAM,
        )
        addresses: list[IPAddress] = []
        for info in infos:
            address = ipaddress.ip_address(info[4][0])
            if address not in addresses:
                addresses.append(address)
        return tuple(addresses)


def validate_oauth_endpoint(url: str, *, allowed_ports: frozenset[int] = frozenset({443})) -> str:
    """Reject ambiguous or non-HTTPS credential/security endpoints。"""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise OAuthOutboundPolicyError() from exc
    hostname = parsed.hostname
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or "%" in hostname
        or (port or 443) not in allowed_ports
        or "\\" in parsed.netloc
    ):
        raise OAuthOutboundPolicyError()
    try:
        hostname.encode("idna")
    except UnicodeError as exc:
        raise OAuthOutboundPolicyError() from exc
    return url


def validate_resolved_addresses(
    addresses: Iterable[IPAddress],
    *,
    allowed_private_cidrs: tuple[IPNetwork, ...],
) -> tuple[IPAddress, ...]:
    """Reject the whole DNS answer set if any address is outside policy。"""
    resolved = tuple(addresses)
    if not resolved:
        raise OAuthOutboundPolicyError()
    for address in resolved:
        if address.is_global:
            continue
        if any(
            address.version == network.version and address in network
            for network in allowed_private_cidrs
        ):
            continue
        raise OAuthOutboundPolicyError()
    return resolved


class PinnedOAuthNetworkBackend:
    """Resolve/validate each new connection, then connect only to that frozen set。"""

    def __init__(
        self,
        *,
        resolver: OAuthResolver,
        backend: AsyncNetworkBackend,
        allowed_private_cidrs: tuple[IPNetwork, ...],
    ) -> None:
        self._resolver = resolver
        self._backend = backend
        self._allowed_private_cidrs = allowed_private_cidrs

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ) -> AsyncNetworkStream:
        addresses = validate_resolved_addresses(
            await self._resolver.resolve(host, port),
            allowed_private_cidrs=self._allowed_private_cidrs,
        )
        last_error: Exception | None = None
        for address in addresses:
            try:
                return await self._backend.connect_tcp(
                    str(address),
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise OAuthOutboundPolicyError()

    async def connect_unix_socket(self, *args, **kwargs):
        raise OAuthOutboundPolicyError()

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class _CoreResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream: Any) -> None:
        self._stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._stream:
            yield chunk

    async def aclose(self) -> None:
        close = getattr(self._stream, "aclose", None)
        if close is not None:
            await close()


class PinnedOAuthTransport(httpx.AsyncBaseTransport):
    """httpx transport backed by a DNS-validated, address-pinned httpcore pool。"""

    def __init__(
        self,
        *,
        resolver: OAuthResolver | None = None,
        allowed_private_cidrs: tuple[IPNetwork, ...] = (),
    ) -> None:
        self._resolver = resolver or SystemOAuthResolver()
        self._fixed_private_cidrs = allowed_private_cidrs
        self._pools: dict[tuple[str, ...], httpcore.AsyncConnectionPool] = {}

    def _pool_for(
        self,
        allowed_private_cidrs: tuple[IPNetwork, ...],
    ) -> httpcore.AsyncConnectionPool:
        key = tuple(str(network) for network in allowed_private_cidrs)
        existing = self._pools.get(key)
        if existing is not None:
            return existing
        backend = PinnedOAuthNetworkBackend(
            resolver=self._resolver,
            backend=httpcore.AnyIOBackend(),
            allowed_private_cidrs=allowed_private_cidrs,
        )
        pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(),
            max_connections=10,
            max_keepalive_connections=0,
            http1=True,
            http2=False,
            retries=0,
            network_backend=backend,
        )
        self._pools[key] = pool
        return pool

    def _request_private_cidrs(self, request: httpx.Request) -> tuple[IPNetwork, ...]:
        raw = request.extensions.get("touka_oauth_private_cidrs", ())
        if raw and self._fixed_private_cidrs:
            raise OAuthOutboundPolicyError()
        if not isinstance(raw, tuple) or any(not isinstance(item, str) for item in raw):
            raise OAuthOutboundPolicyError()
        try:
            requested = tuple(ipaddress.ip_network(item, strict=True) for item in raw)
        except ValueError as exc:
            raise OAuthOutboundPolicyError() from exc
        if any(network.prefixlen == 0 for network in requested):
            raise OAuthOutboundPolicyError()
        return requested or self._fixed_private_cidrs

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not isinstance(request.stream, httpx.AsyncByteStream):
            raise OAuthUpstreamError()
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        pool = self._pool_for(self._request_private_cidrs(request))
        response = await pool.handle_async_request(core_request)
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_CoreResponseStream(response.stream),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await asyncio.gather(*(pool.aclose() for pool in self._pools.values()))


def create_hardened_oauth_client(
    *,
    resolver: OAuthResolver | None = None,
    allowed_private_cidrs: tuple[IPNetwork, ...] = (),
) -> httpx.AsyncClient:
    """Create the no-proxy/no-redirect HTTPS client used for all OAuth egress。"""
    return httpx.AsyncClient(
        transport=PinnedOAuthTransport(
            resolver=resolver,
            allowed_private_cidrs=allowed_private_cidrs,
        ),
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(
            connect=OAUTH_HTTP_CONNECT_TIMEOUT_SECONDS,
            read=OAUTH_HTTP_READ_TIMEOUT_SECONDS,
            write=OAUTH_HTTP_WRITE_TIMEOUT_SECONDS,
            pool=OAUTH_HTTP_POOL_TIMEOUT_SECONDS,
        ),
    )


def _validate_json_shape(value: Any, *, depth: int = 0) -> int:
    if depth > OAUTH_HTTP_MAX_JSON_DEPTH:
        raise OAuthUpstreamError()
    if isinstance(value, dict):
        count = len(value)
        for key, item in value.items():
            if not isinstance(key, str):
                raise OAuthUpstreamError()
            count += _validate_json_shape(item, depth=depth + 1)
    elif isinstance(value, list):
        count = len(value)
        for item in value:
            count += _validate_json_shape(item, depth=depth + 1)
    else:
        count = 1
    if count > OAUTH_HTTP_MAX_JSON_ITEMS:
        raise OAuthUpstreamError()
    return count


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OAuthTokenProtocolError()
        result[key] = value
    return result


class OAuthHttpClient:
    """Bounded OAuth metadata/token/revocation HTTP client。"""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        epoch_clock: Callable[[], float] = get_current_epoch,
    ) -> None:
        self._client = client
        self._epoch_clock = epoch_clock

    async def _request_bytes(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, str] | None = None,
        json_data: dict[str, Any] | None = None,
        auth: httpx.Auth | None = None,
        expected_statuses: frozenset[int] = frozenset({200}),
        private_network_policy: OAuthPrivateNetworkPolicy | None = None,
    ) -> tuple[bytes, str, int]:
        validate_oauth_endpoint(url)
        allowed_private_cidrs = (
            private_network_policy(url) if private_network_policy is not None else ()
        )
        try:
            async with self._client.stream(
                method,
                url,
                data=data,
                json=json_data,
                auth=auth,
                follow_redirects=False,
                extensions={
                    "touka_oauth_private_cidrs": tuple(
                        str(network) for network in allowed_private_cidrs
                    )
                },
            ) as response:
                if response.status_code not in expected_statuses or response.is_redirect:
                    raise OAuthUpstreamError()
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > OAUTH_HTTP_MAX_RESPONSE_BYTES:
                        raise OAuthUpstreamError()
                    chunks.append(chunk)
                return (
                    b"".join(chunks),
                    response.headers.get("content-type", ""),
                    response.status_code,
                )
        except OAuthUpstreamError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            raise OAuthTransportError() from exc

    async def get_json(
        self,
        url: str,
        *,
        private_network_policy: OAuthPrivateNetworkPolicy | None = None,
    ) -> dict[str, Any]:
        body, content_type, _status = await self._request_bytes(
            "GET",
            url,
            private_network_policy=private_network_policy,
        )
        if content_type.partition(";")[0].strip().lower() != "application/json":
            raise OAuthUpstreamError()
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise OAuthUpstreamError() from exc
        if not isinstance(parsed, dict):
            raise OAuthUpstreamError()
        _validate_json_shape(parsed)
        return parsed

    async def _post_json(
        self,
        url: str,
        data: dict[str, str],
        *,
        auth: httpx.Auth | None = None,
        private_network_policy: OAuthPrivateNetworkPolicy | None = None,
    ) -> dict[str, Any]:
        body, content_type, status = await self._request_bytes(
            "POST",
            url,
            data=data,
            auth=auth,
            expected_statuses=frozenset({200, 400}),
            private_network_policy=private_network_policy,
        )
        if content_type.partition(";")[0].strip().lower() != "application/json":
            raise OAuthUpstreamError()
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise OAuthUpstreamError() from exc
        if not isinstance(parsed, dict):
            raise OAuthUpstreamError()
        _validate_json_shape(parsed)
        if status == 400:
            if parsed.get("error") == "invalid_grant":
                raise OAuthInvalidGrantError()
            raise OAuthUpstreamError()
        return parsed

    async def post_client_credentials_token(
        self,
        *,
        token_endpoint: str,
        form: dict[str, str],
        basic_auth: tuple[str, str] | None,
        private_network_policy: OAuthPrivateNetworkPolicy | None = None,
    ) -> tuple[dict[str, Any], int]:
        """Post one bounded client-credentials form without interpreting tokens。"""
        auth = httpx.BasicAuth(*basic_auth) if basic_auth is not None else None
        body, content_type, status = await self._request_bytes(
            "POST",
            token_endpoint,
            data=form,
            auth=auth,
            expected_statuses=frozenset({200, *range(400, 600)}),
            private_network_policy=private_network_policy,
        )
        if status != 200:
            return {}, status
        if content_type.partition(";")[0].strip().lower() != "application/json":
            raise OAuthTokenProtocolError()
        try:
            parsed = json.loads(body, object_pairs_hook=_reject_duplicate_json_keys)
        except OAuthTokenProtocolError:
            raise
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise OAuthTokenProtocolError() from exc
        if not isinstance(parsed, dict):
            raise OAuthTokenProtocolError()
        try:
            _validate_json_shape(parsed)
        except OAuthUpstreamError as exc:
            raise OAuthTokenProtocolError() from exc
        return parsed, status

    def _token_set(self, payload: dict[str, Any]) -> tuple[OAuthTokenSet, str | None]:
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        token_type = payload.get("token_type")
        expires_in = payload.get("expires_in")
        scope = payload.get("scope")
        if (
            not isinstance(access_token, str)
            or not access_token
            or refresh_token is not None
            and (not isinstance(refresh_token, str) or not refresh_token)
            or not isinstance(token_type, str)
            or token_type.lower() != "bearer"
            or isinstance(expires_in, bool)
            or not isinstance(expires_in, (int, float))
            or expires_in <= 0
            or scope is not None
            and not isinstance(scope, str)
        ):
            raise OAuthUpstreamError()
        return (
            OAuthTokenSet(
                access_token=SecretStr(access_token),
                refresh_token=(SecretStr(refresh_token) if refresh_token else None),
                token_type="Bearer",
                expires_at=self._epoch_clock() + float(expires_in),
                granted_scopes=frozenset(),
            ),
            scope,
        )

    async def exchange_authorization_code(
        self,
        *,
        token_endpoint: str,
        code: str,
        code_verifier: str,
        client_id: str,
        redirect_uri: str,
        resource: str,
        client_secret: str | None = None,
        token_endpoint_auth_method: str = "none",
        private_network_policy: OAuthPrivateNetworkPolicy | None = None,
    ) -> tuple[OAuthTokenSet, str | None]:
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": code_verifier,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "resource": resource,
        }
        auth = self._bind_client_auth(
            form,
            client_secret=client_secret,
            token_endpoint_auth_method=token_endpoint_auth_method,
        )
        payload = await self._post_json(
            token_endpoint,
            form,
            auth=auth,
            private_network_policy=private_network_policy,
        )
        return self._token_set(payload)

    async def refresh_access_token(
        self,
        *,
        token_endpoint: str,
        refresh_token: str,
        client_id: str,
        resource: str,
        client_secret: str | None = None,
        token_endpoint_auth_method: str = "none",
        private_network_policy: OAuthPrivateNetworkPolicy | None = None,
    ) -> tuple[OAuthTokenSet, str | None]:
        form = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "resource": resource,
        }
        auth = self._bind_client_auth(
            form,
            client_secret=client_secret,
            token_endpoint_auth_method=token_endpoint_auth_method,
        )
        payload = await self._post_json(
            token_endpoint,
            form,
            auth=auth,
            private_network_policy=private_network_policy,
        )
        return self._token_set(payload)

    async def refresh_prepared_access_token(
        self,
        request: OAuthPreparedRefreshRequest,
        *,
        private_network_policy: OAuthPrivateNetworkPolicy | None = None,
    ) -> tuple[OAuthTokenSet, str | None]:
        """Send the exact immutable refresh request already bound by its permit。"""
        basic_auth = request.basic_auth_values()
        auth = httpx.BasicAuth(*basic_auth) if basic_auth is not None else None
        payload = await self._post_json(
            request.endpoint,
            request.form_data(),
            auth=auth,
            private_network_policy=private_network_policy,
        )
        return self._token_set(payload)

    @staticmethod
    def _bind_client_auth(
        form: dict[str, str],
        *,
        client_secret: str | None,
        token_endpoint_auth_method: str,
    ) -> httpx.Auth | None:
        if token_endpoint_auth_method == "none" and client_secret is None:
            return None
        if token_endpoint_auth_method == "client_secret_basic" and client_secret is not None:
            client_id = form.pop("client_id", None)
            if client_id is None:
                raise OAuthUpstreamError()
            return httpx.BasicAuth(client_id, client_secret)
        if token_endpoint_auth_method != "client_secret_post" or client_secret is None:
            raise OAuthUpstreamError()
        form["client_secret"] = client_secret
        return None

    async def revoke_token(
        self,
        *,
        revocation_endpoint: str,
        token: str,
        client_id: str,
        private_network_policy: OAuthPrivateNetworkPolicy | None = None,
    ) -> None:
        await self._request_bytes(
            "POST",
            revocation_endpoint,
            data={"token": token, "client_id": client_id},
            private_network_policy=private_network_policy,
        )

    async def fetch_client_metadata_document(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        token_endpoint_auth_method: str,
        private_network_policy: OAuthPrivateNetworkPolicy | None = None,
    ) -> OAuthClientBinding:
        """Fetch and exact-validate an HTTPS Client ID Metadata Document。"""
        validate_oauth_endpoint(client_id)
        payload = await self.get_json(
            client_id,
            private_network_policy=private_network_policy,
        )
        if (
            payload.get("client_id") != client_id
            or payload.get("redirect_uris") != [redirect_uri]
            or payload.get("grant_types") != ["authorization_code"]
            or payload.get("response_types") != ["code"]
            or payload.get("token_endpoint_auth_method") != token_endpoint_auth_method
            or "client_secret" in payload
        ):
            raise OAuthUpstreamError()
        return OAuthClientBinding(
            client_id=client_id,
            token_endpoint_auth_method=token_endpoint_auth_method,
            registration_version=1,
        )

    async def register_client(
        self,
        *,
        registration_endpoint: str,
        redirect_uri: str,
        token_endpoint_auth_method: str,
        private_network_policy: OAuthPrivateNetworkPolicy | None = None,
    ) -> OAuthClientBinding:
        """Perform one bounded DCR attempt; management credentials are unsupported。"""
        body, content_type, _status = await self._request_bytes(
            "POST",
            registration_endpoint,
            json_data={
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code"],
                "response_types": ["code"],
                "token_endpoint_auth_method": token_endpoint_auth_method,
                "application_type": "web",
            },
            expected_statuses=frozenset({201}),
            private_network_policy=private_network_policy,
        )
        if content_type.partition(";")[0].strip().lower() != "application/json":
            raise OAuthUpstreamError()
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise OAuthUpstreamError() from exc
        if not isinstance(payload, dict):
            raise OAuthUpstreamError()
        _validate_json_shape(payload)
        client_id = payload.get("client_id")
        returned_method = payload.get("token_endpoint_auth_method")
        secret = payload.get("client_secret")
        secret_expiry = payload.get("client_secret_expires_at")
        if (
            not isinstance(client_id, str)
            or not client_id
            or returned_method != token_endpoint_auth_method
            or "registration_access_token" in payload
            or "registration_client_uri" in payload
            or token_endpoint_auth_method == "none"
            and secret is not None
            or token_endpoint_auth_method == "client_secret_post"
            and (not isinstance(secret, str) or not secret)
            or secret_expiry is not None
            and (isinstance(secret_expiry, bool) or not isinstance(secret_expiry, int))
        ):
            raise OAuthUpstreamError()
        return OAuthClientBinding(
            client_id=client_id,
            token_endpoint_auth_method=token_endpoint_auth_method,
            client_secret=SecretStr(secret) if isinstance(secret, str) else None,
            client_secret_expires_at=(
                float(secret_expiry) if isinstance(secret_expiry, int) and secret_expiry else None
            ),
            registration_version=1,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def _well_known_url(origin_url: str, suffix: str) -> str:
    parsed = urlsplit(validate_oauth_endpoint(origin_url))
    path = parsed.path if parsed.path != "/" else ""
    return f"{parsed.scheme}://{parsed.netloc}/.well-known/{suffix}{path}"


def _string_tuple(payload: dict[str, Any], field: str) -> tuple[str, ...]:
    value = payload.get(field)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise OAuthMetadataDiscoveryError()
    return tuple(value)


class OAuthMetadataDiscovery:
    """Exact RFC 9728/RFC 8414 metadata discovery and security fingerprinting。"""

    def __init__(self, client: OAuthHttpClient) -> None:
        self._client = client

    async def discover(
        self,
        *,
        resource: str,
        profile: Any,
        selected_issuer: str,
        installation_id: str | None = None,
    ) -> OAuthAuthorizationMetadata:
        if selected_issuer not in profile.trusted_issuers:
            raise OAuthMetadataDiscoveryError()
        private_network_policy = (
            (lambda url: profile.allowed_private_cidrs(installation_id, url))
            if installation_id is not None
            else None
        )
        protected = await self._client.get_json(
            _well_known_url(resource, "oauth-protected-resource"),
            private_network_policy=private_network_policy,
        )
        if protected.get("resource") != resource:
            raise OAuthMetadataDiscoveryError()
        authorization_servers = _string_tuple(protected, "authorization_servers")
        if selected_issuer not in authorization_servers:
            raise OAuthMetadataDiscoveryError()
        authorization = await self._client.get_json(
            _well_known_url(selected_issuer, "oauth-authorization-server"),
            private_network_policy=private_network_policy,
        )
        if authorization.get("issuer") != selected_issuer:
            raise OAuthMetadataDiscoveryError()
        authorization_endpoint = authorization.get("authorization_endpoint")
        token_endpoint = authorization.get("token_endpoint")
        revocation_endpoint = authorization.get("revocation_endpoint")
        registration_endpoint = authorization.get("registration_endpoint")
        if not isinstance(authorization_endpoint, str) or not isinstance(token_endpoint, str):
            raise OAuthMetadataDiscoveryError()
        validate_oauth_endpoint(authorization_endpoint)
        validate_oauth_endpoint(token_endpoint)
        if revocation_endpoint is not None:
            if not isinstance(revocation_endpoint, str):
                raise OAuthMetadataDiscoveryError()
            validate_oauth_endpoint(revocation_endpoint)
        if registration_endpoint is not None:
            if not isinstance(registration_endpoint, str):
                raise OAuthMetadataDiscoveryError()
            validate_oauth_endpoint(registration_endpoint)
        code_methods = _string_tuple(
            authorization,
            "code_challenge_methods_supported",
        )
        response_modes = _string_tuple(authorization, "response_modes_supported")
        iss_supported = authorization.get("authorization_response_iss_parameter_supported")
        if not isinstance(iss_supported, bool):
            raise OAuthMetadataDiscoveryError()
        security_projection = {
            "protected": protected,
            "authorization": authorization,
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                security_projection,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        return OAuthAuthorizationMetadata(
            issuer=selected_issuer,
            authorization_endpoint=authorization_endpoint,
            token_endpoint=token_endpoint,
            revocation_endpoint=revocation_endpoint,
            registration_endpoint=registration_endpoint,
            code_challenge_methods_supported=code_methods,
            response_modes_supported=response_modes,
            authorization_response_iss_parameter_supported=iss_supported,
            security_fingerprint=fingerprint,
        )
