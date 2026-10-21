"""Real PyJWT/urllib3 advisory primitives with synthetic keys, streams and no network."""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import http.client
import io
import json
import socket
import ssl
import urllib.request
from email.message import Message
from typing import Any
from unittest.mock import MagicMock
from urllib.response import addinfourl

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from jwt import PyJWK, PyJWKClient
from jwt import decode as decode_jwt
from jwt.algorithms import HMACAlgorithm, RSAAlgorithm
from jwt.exceptions import InvalidKeyError, InvalidSignatureError, PyJWKClientConnectionError
from urllib3 import ProxyManager
from urllib3 import connection as urllib3_connection
from urllib3.exceptions import ProtocolError
from urllib3.response import HTTPResponse


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transport fakes must intercept all I/O; an accidental live connection is a failure."""

    def refuse_network(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Security primitive regression attempted a real network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse_network)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse_network)
    monkeypatch.setattr(socket, "create_connection", refuse_network)


@pytest.fixture(scope="module")
def synthetic_rsa_key() -> rsa.RSAPrivateKey:
    """An ephemeral fixture key never represents a user or a service identity."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def hmac_algorithm() -> HMACAlgorithm:
    return HMACAlgorithm(hashlib.sha256)


@pytest.mark.parametrize("loader", ["algorithm", "pyjwk"])
def test_cve_2026_102266_empty_oct_jwk_is_rejected(loader: str) -> None:
    """The affected from_jwk route, not the already-fixed raw empty bytes route."""
    jwk = {"kty": "oct", "k": "", "alg": "HS256"}
    with pytest.raises(InvalidKeyError):
        if loader == "algorithm":
            HMACAlgorithm.from_jwk(jwk)
        else:
            PyJWK.from_dict(jwk)


def test_nonempty_oct_jwk_preserves_secret_bytes(hmac_algorithm: HMACAlgorithm) -> None:
    secret = b"synthetic-hmac-fixture-32-bytes!!!"
    jwk = HMACAlgorithm.to_jwk(secret, as_dict=True)
    assert HMACAlgorithm.from_jwk(jwk) == secret
    assert PyJWK.from_dict(jwk).key == secret
    assert hmac_algorithm.prepare_key(secret) == secret


@pytest.mark.parametrize("mutation", ["tab-before-end", "cr-only", "single-line"])
def test_cve_2026_102268_loader_valid_mutated_pem_is_not_an_hmac_secret(
    synthetic_rsa_key: rsa.RSAPrivateKey, hmac_algorithm: HMACAlgorithm, mutation: str
) -> None:
    public = synthetic_rsa_key.public_key()
    pem = public.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    if mutation == "tab-before-end":
        mutated = pem.replace(b"-----END", b"\t-----END")
    elif mutation == "cr-only":
        mutated = pem.replace(b"\n", b"\r")
    else:
        mutated = pem.replace(b"\n", b"")
    assert serialization.load_pem_public_key(mutated).public_numbers() == public.public_numbers()
    with pytest.raises(InvalidKeyError):
        hmac_algorithm.prepare_key(mutated)


@pytest.mark.parametrize("encoding", ["spki", "pkcs1", "certificate"])
def test_cve_2026_102271_der_public_material_is_not_an_hmac_secret(
    synthetic_rsa_key: rsa.RSAPrivateKey, hmac_algorithm: HMACAlgorithm, encoding: str
) -> None:
    public = synthetic_rsa_key.public_key()
    if encoding == "certificate":
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "owned-fixture.invalid")])
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(public)
            .serial_number(1)
            .not_valid_before(datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC))
            .not_valid_after(datetime.datetime(2030, 1, 1, tzinfo=datetime.UTC))
            .sign(synthetic_rsa_key, hashes.SHA256())
        )
        der = certificate.public_bytes(serialization.Encoding.DER)
        assert (
            x509.load_der_x509_certificate(der).public_key().public_numbers()
            == public.public_numbers()
        )
    else:
        fmt = (
            serialization.PublicFormat.SubjectPublicKeyInfo
            if encoding == "spki"
            else serialization.PublicFormat.PKCS1
        )
        der = public.public_bytes(serialization.Encoding.DER, fmt)
        assert serialization.load_der_public_key(der).public_numbers() == public.public_numbers()
    with pytest.raises(InvalidKeyError):
        hmac_algorithm.prepare_key(der)


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "utf-16-le", "utf-32", "utf-32-be"])
def test_cve_2026_102272_encoded_public_jwk_is_not_an_hmac_secret(
    synthetic_rsa_key: rsa.RSAPrivateKey, hmac_algorithm: HMACAlgorithm, encoding: str
) -> None:
    jwk = RSAAlgorithm.to_jwk(synthetic_rsa_key.public_key(), as_dict=True)
    encoded = json.dumps(jwk).encode(encoding)
    assert json.loads(encoded) == jwk
    with pytest.raises(InvalidKeyError):
        hmac_algorithm.prepare_key(encoded)


@pytest.mark.parametrize("container", ["jwks", "array", "nested", "escaped-key", "utf16-nested"])
def test_cve_2026_102273_nested_public_jwk_is_not_an_hmac_secret(
    synthetic_rsa_key: rsa.RSAPrivateKey, hmac_algorithm: HMACAlgorithm, container: str
) -> None:
    jwk = RSAAlgorithm.to_jwk(synthetic_rsa_key.public_key(), as_dict=True)
    if container == "jwks":
        value: Any = {"keys": [jwk]}
    elif container == "array":
        value = [jwk]
    else:
        value = {"outer": [{"public": jwk}]}
    raw = json.dumps(value)
    if container == "escaped-key":
        raw = raw.replace('"kty"', '"\\u006bty"')
    encoded = raw.encode("utf-16" if container == "utf16-nested" else "utf-8")
    assert json.loads(encoded) == value
    with pytest.raises(InvalidKeyError):
        hmac_algorithm.prepare_key(encoded)


@pytest.mark.parametrize(
    "value",
    [
        b"ordinary-binary-secret-32-bytes!!\x00\xff",
        b'{"secret":"synthetic ordinary JSON key material"}',
        b'["kty", "RSA", "ordinary strings are not a public JWK"]',
    ],
)
def test_nonpublic_hmac_material_is_preserved_exactly(
    hmac_algorithm: HMACAlgorithm, value: bytes
) -> None:
    assert hmac_algorithm.prepare_key(value) == value


def _memory_jwks_transport(monkeypatch: pytest.MonkeyPatch, *, redirect: bool) -> list[str]:
    """Use urllib's real redirect chain; replace only its HTTPS transport with finite responses."""
    observed: list[str] = []
    origin = "https://jwks-origin.invalid/keys"
    destination = "https://redirect-destination.invalid/keys"

    class MemoryHTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, request: urllib.request.Request) -> addinfourl:
            observed.append(request.full_url)
            assert request.full_url in {origin, destination}
            headers = Message()
            if request.full_url == origin and redirect:
                headers["Location"] = destination
                response = addinfourl(io.BytesIO(b""), headers, request.full_url, 302)
                response.msg = "Found"
            else:
                response = addinfourl(io.BytesIO(b'{"keys": []}'), headers, request.full_url, 200)
                response.msg = "OK"
            return response

    class ClosedProxyHandler(urllib.request.ProxyHandler):
        def __init__(self) -> None:
            super().__init__({})

    monkeypatch.setattr(urllib.request, "HTTPSHandler", MemoryHTTPSHandler)
    monkeypatch.setattr(urllib.request, "ProxyHandler", ClosedProxyHandler)
    monkeypatch.setattr(urllib.request, "_opener", None)
    return observed


def test_cve_2026_102267_jwks_redirect_is_refused_before_destination_contact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = _memory_jwks_transport(monkeypatch, redirect=True)
    client = PyJWKClient(
        "https://jwks-origin.invalid/keys",
        cache_jwk_set=False,
        ssl_context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        timeout=1,
    )
    try:
        with pytest.raises(PyJWKClientConnectionError):
            client.fetch_data()
    finally:
        assert observed == ["https://jwks-origin.invalid/keys"]


def test_direct_jwks_response_still_loads_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    observed = _memory_jwks_transport(monkeypatch, redirect=False)
    client = PyJWKClient(
        "https://jwks-origin.invalid/keys",
        cache_jwk_set=False,
        ssl_context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        timeout=1,
    )
    assert client.fetch_data() == {"keys": []}
    assert observed == ["https://jwks-origin.invalid/keys"]


@pytest.mark.parametrize("forwarding", [False, True], ids=["connect", "forwarding"])
def test_cve_2026_97687_target_policy_cannot_mutate_or_replace_proxy_tls_policy(
    monkeypatch: pytest.MonkeyPatch, forwarding: bool
) -> None:
    """Keep real connection/TLS policy selection; mock the socket handshake boundary only."""
    proxy_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    proxy_context.check_hostname = False
    proxy_context.verify_mode = ssl.CERT_REQUIRED
    target_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    target_context.check_hostname = False
    target_context.verify_mode = ssl.CERT_NONE
    transport = MagicMock()
    transport.getpeercert.return_value = {"subjectAltName": (("DNS", "proxy.invalid"),)}
    transport.selected_alpn_protocol.return_value = None
    handshakes: list[dict[str, Any]] = []

    def wrap_socket(**kwargs: Any) -> Any:
        handshakes.append(dict(kwargs))
        return transport

    monkeypatch.setattr(urllib3_connection.HTTPSConnection, "_new_conn", lambda self: transport)
    monkeypatch.setattr(urllib3_connection.HTTPSConnection, "_tunnel", lambda self: None)
    monkeypatch.setattr(urllib3_connection, "ssl_wrap_socket", wrap_socket)
    manager = ProxyManager(
        "https://proxy.invalid",
        proxy_ssl_context=proxy_context,
        use_forwarding_for_https=forwarding,
        ssl_context=target_context,
        cert_reqs="CERT_NONE",
        server_hostname="target.invalid",
        cert_file="synthetic-target-cert.pem",
        key_file="synthetic-target-key.pem",
    )
    pool = manager.connection_from_url("https://target.invalid/")
    connection = pool._new_conn()
    if not forwarding:
        connection.set_tunnel("target.invalid", 443, scheme="https")
    try:
        connection.connect()
        assert proxy_context.verify_mode == ssl.CERT_REQUIRED
        assert handshakes[0]["ssl_context"] is proxy_context
        assert handshakes[0]["server_hostname"] == "proxy.invalid"
        assert handshakes[0]["certfile"] is None and handshakes[0]["keyfile"] is None
        assert connection.proxy_is_verified is True
        assert len(handshakes) == (1 if forwarding else 2)
        if not forwarding:
            assert handshakes[1]["ssl_context"] is target_context
            assert handshakes[1]["server_hostname"] == "target.invalid"
        assert target_context.verify_mode == ssl.CERT_NONE
    finally:
        connection.close()
        manager.clear()


class ObservedLines(io.BytesIO):
    """Measure the actual readline bound and bytes returned to the library."""

    def __init__(self, raw: bytes) -> None:
        super().__init__(raw)
        self.calls: list[tuple[int, int]] = []

    def readline(self, size: int = -1) -> bytes:
        line = super().readline(size)
        self.calls.append((size, len(line)))
        return line


def _chunked_response(body: bytes) -> tuple[HTTPResponse, ObservedLines]:
    raw = ObservedLines(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + body)
    fake_socket = MagicMock()
    fake_socket.makefile.return_value = raw
    original = http.client.HTTPResponse(fake_socket, method="GET")
    original.begin()
    raw.calls.clear()
    response = HTTPResponse(
        body=original,
        headers=list(original.headers.items()),
        original_response=original,
        preload_content=False,
        decode_content=False,
    )
    return response, raw


@pytest.mark.parametrize("api", ["read_chunked", "stream"])
def test_cve_2026_97689_chunk_size_line_has_a_finite_read_bound(api: str) -> None:
    """A finite 65 KiB fixture exposes unbounded readline without exhausting resources."""
    response, raw = _chunked_response(b"f" * 65538 + b"\r\n")
    try:
        with pytest.raises((ProtocolError, http.client.LineTooLong)):
            list(response.read_chunked(amt=1) if api == "read_chunked" else response.stream(amt=1))
        assert raw.calls, "The real chunk-size parser must read its stream"
        assert all(0 < limit <= 65537 and length <= 65537 for limit, length in raw.calls)
    finally:
        response.close()


def test_short_chunked_stream_is_preserved() -> None:
    response, _raw = _chunked_response(b"4\r\ntest\r\n0\r\n\r\n")
    try:
        assert b"".join(response.stream(amt=2)) == b"test"
    finally:
        response.close()


def _synthetic_signed_token(*, padded: bool) -> tuple[bytes, bytes]:
    """Build the compact JWS independently, including its signature over exact padding."""
    secret = b"synthetic-fixture-hmac-secret-32-bytes"
    header = b'{"alg":"HS256","typ":"JWT","kid":"fixture"}'
    payload = b'{"sub":"fixture","note":"ordinary"}'
    segments = [base64.urlsafe_b64encode(value) for value in (header, payload)]
    if padded:
        assert any(segment.endswith(b"=") for segment in segments)
    else:
        segments = [segment.rstrip(b"=") for segment in segments]
    signing_input = b".".join(segments)
    signature = base64.urlsafe_b64encode(hmac.digest(secret, signing_input, "sha256"))
    return signing_input + b"." + (signature if padded else signature.rstrip(b"=")), secret


@pytest.mark.parametrize("padded", [False, True], ids=["unpadded", "trailing-padding"])
def test_signed_token_with_standard_base64url_padding_is_preserved(padded: bool) -> None:
    token, secret = _synthetic_signed_token(padded=padded)
    assert decode_jwt(token, secret, algorithms=["HS256"]) == {
        "sub": "fixture",
        "note": "ordinary",
    }


@pytest.mark.parametrize("padded", [False, True], ids=["unpadded", "trailing-padding"])
def test_padding_compatibility_still_requires_the_correct_signature(padded: bool) -> None:
    token, _secret = _synthetic_signed_token(padded=padded)
    with pytest.raises(InvalidSignatureError):
        decode_jwt(token, b"different-synthetic-hmac-secret-32-bytes", algorithms=["HS256"])
