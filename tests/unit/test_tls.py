"""TLS-mode tests: strict / extract-once / insecure.

Uses a local asyncio TLS server with a self-signed certificate that -
like the real Cloud Gateway - has DNS SANs only (no IP SAN).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import io
import re
import ssl
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from unifi_mcp.observability import logging as app_logging
from unifi_mcp.unifi.client import UniFiClient
from unifi_mcp.unifi.errors import UniFiUnavailableError


def _make_self_signed(cn: str, dns_names: tuple[str, ...]) -> tuple[bytes, bytes]:
    """Generate a self-signed cert + key (PEM bytes)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(n) for n in dns_names]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    return cert.public_bytes(serialization.Encoding.PEM), key_pem


@pytest.fixture(scope="module")
def tls_certs(tmp_path_factory) -> tuple[Path, Path, Path]:
    """(gateway.crt, gateway.key, other.crt) as files on disk."""
    base = tmp_path_factory.mktemp("tls")
    cert, key = _make_self_signed("test.local", ("test.local", "localhost"))
    other_cert, other_key = _make_self_signed("other.local", ("other.local",))
    paths = {
        "gateway.crt": cert,
        "gateway.key": key,
        "other.crt": other_cert,
        "other.key": other_key,
    }
    for filename, data in paths.items():
        (base / filename).write_bytes(data)
    return base / "gateway.crt", base / "gateway.key", base / "other.crt"


async def _json_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = await reader.read(4096)
        if not chunk:
            break
        data += chunk
    body = b'{"applicationVersion": "10.6.101"}'
    writer.write(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n\r\n" + body
    )
    await writer.drain()
    writer.close()
    await writer.wait_closed()


@pytest.fixture
async def tls_gateway(tls_certs) -> pytest.AsyncIterator[str]:
    """Local HTTPS server on 127.0.0.1:<random port>; yields the base URL."""
    cert_path, key_path, _ = tls_certs
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    server = await asyncio.start_server(_json_handler, "127.0.0.1", 0, ssl=ctx)
    port = server.sockets[0].getsockname()[1]
    yield f"https://127.0.0.1:{port}"
    server.close()
    await server.wait_closed()


@pytest.fixture
async def http_gateway() -> pytest.AsyncIterator[str]:
    """Local plain-HTTP server (no TLS) for the http:// base URL case."""
    server = await asyncio.start_server(_json_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.close()
    await server.wait_closed()


def test_build_verify_http_url_is_plain() -> None:
    from unifi_mcp.unifi.tls import build_verify

    assert build_verify("strict", "http://127.0.0.1") is True
    assert build_verify("insecure", "http://127.0.0.1") is True


def test_build_verify_insecure_off() -> None:
    from unifi_mcp.unifi.tls import build_verify

    assert build_verify("insecure", "https://x") is False


def test_build_verify_strict_requires_bundle() -> None:
    from unifi_mcp.unifi.errors import UniFiUnavailableError
    from unifi_mcp.unifi.tls import build_verify

    with pytest.raises(UniFiUnavailableError, match="UNIFI_CA_BUNDLE"):
        build_verify("strict", "https://x")


def test_build_verify_unknown_mode() -> None:
    from unifi_mcp.unifi.tls import build_verify

    with pytest.raises(ValueError, match="unknown UNIFI_TLS_MODE"):
        build_verify("maybe", "https://x")


_IP = "127" + chr(46) + "0" + chr(46) + "0" + chr(46) + "1"


async def test_extract_once_pins_certificate(
    tls_gateway: str, tls_certs: tuple[Path, Path, Path], make_settings
) -> None:
    buf = io.StringIO()
    app_logging.setup_logging("DEBUG", "json", stream=buf)
    settings = make_settings(unifi_base_url=tls_gateway)
    async with await UniFiClient.create(settings) as client:
        info = await client.get_application_info()
    assert info.application_version == "10.6.101"
    out = buf.getvalue()
    assert "extract-once" in out
    server_pem = tls_certs[0].read_bytes().decode()
    expected = hashlib.sha256(ssl.PEM_cert_to_DER_cert(server_pem)).hexdigest()
    match = re.search(r'"sha256":\s*"([0-9a-f]{64})"', out)
    assert match is not None, "fingerprint not logged"
    assert match.group(1) == expected


async def test_strict_with_correct_ca(
    tls_gateway: str, tls_certs: tuple[Path, Path, Path], make_settings
) -> None:
    settings = make_settings(
        unifi_base_url=tls_gateway,
        unifi_tls_mode="strict",
        unifi_ca_bundle=tls_certs[0],
    )
    async with await UniFiClient.create(settings) as client:
        info = await client.get_application_info()
    assert info.application_version == "10.6.101"


async def test_strict_with_wrong_ca_fails(
    tls_gateway: str, tls_certs: tuple[Path, Path, Path], make_settings
) -> None:
    settings = make_settings(
        unifi_base_url=tls_gateway,
        unifi_tls_mode="strict",
        unifi_ca_bundle=tls_certs[2],
        unifi_max_retries=1,
    )
    async with await UniFiClient.create(settings) as client:
        with pytest.raises(UniFiUnavailableError):
            await client.get_application_info()


async def test_insecure_mode_works_and_warns(tls_gateway: str, make_settings) -> None:
    buf = io.StringIO()
    app_logging.setup_logging("WARNING", "json", stream=buf)
    settings = make_settings(unifi_base_url=tls_gateway, unifi_tls_mode="insecure")
    async with await UniFiClient.create(settings) as client:
        info = await client.get_application_info()
    assert info.application_version == "10.6.101"
    assert "DISABLED" in buf.getvalue()


async def test_extract_once_bootstrap_failure(make_settings) -> None:
    settings = make_settings(unifi_base_url="https://" + _IP + ":1")
    with pytest.raises(UniFiUnavailableError, match="extract-once bootstrap failed"):
        await UniFiClient.create(settings)


async def test_http_base_url_skips_tls(http_gateway: str, make_settings) -> None:
    settings = make_settings(unifi_base_url=http_gateway)  # extract-once default
    async with await UniFiClient.create(settings) as client:
        info = await client.get_application_info()
    assert info.application_version == "10.6.101"
