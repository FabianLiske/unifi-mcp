"""TLS handling for the UniFi gateway (strict / extract-once / insecure).

Design decision (verified live against the local Cloud Gateway): the gateway's
self-signed certificate carries SANs for ``unifi.local``/``localhost``/loopback
only, **not** for the LAN IP it is reached via. Full hostname verification
therefore can never succeed against an IP-based ``UNIFI_BASE_URL``. The client
hence always sets ``check_hostname=False`` on its SSL context: chain
verification against the (pinned) CA bundle still enforces the exact
certificate, while the hostname mismatch is logged as a prominent warning.
"""

from __future__ import annotations

import hashlib
import socket
import ssl
from urllib.parse import urlsplit

from unifi_mcp.observability.logging import get_logger
from unifi_mcp.unifi.errors import UniFiUnavailableError

logger = get_logger(__name__)


def bootstrap_extract_once(host: str, port: int, connect_timeout: float) -> tuple[str, str]:
    """Perform the one-time unverified TLS handshake of ``extract-once``.

    Fetches the peer certificate and returns it as ``(pem, sha256_hex)``.
    This is intentionally unverified: the purpose is to *pin* the certificate
    for the rest of the session, with the fingerprint logged so an operator
    can notice certificate rotation or a MITM.

    Synchronous; callers must run it via ``asyncio.to_thread``.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with (
        socket.create_connection((host, port), timeout=connect_timeout) as sock,
        ctx.wrap_socket(sock, server_hostname=host) as tls_sock,
    ):
        der = tls_sock.getpeercert(binary_form=True)
    if not der:
        msg = "TLS extract-once bootstrap failed: peer presented no certificate"
        raise UniFiUnavailableError(msg)
    pem = ssl.DER_cert_to_PEM_cert(der)
    fingerprint = hashlib.sha256(der).hexdigest()
    return pem, fingerprint


def build_verify(
    mode: str,
    base_url: str,
    *,
    ca_bundle: str | None = None,
    extracted_pem: str | None = None,
) -> bool | ssl.SSLContext:
    """Build the ``httpx.AsyncClient(verify=...)`` argument for a TLS mode.

    - ``strict``: verify the chain against ``ca_bundle`` (path).
    - ``extract-once``: verify the chain against ``extracted_pem`` (the
      bootstrap certificate, acting as its own self-signed CA).
    - ``insecure``: verification off, prominent warning.
    - ``http://`` base URLs: no TLS involved, plain ``True``.
    """
    if urlsplit(base_url).scheme != "https":
        return True
    if mode == "insecure":
        logger.warning(
            "TLS verification DISABLED (UNIFI_TLS_MODE=insecure) - only use for local development"
        )
        return False
    ctx = ssl.create_default_context()
    if mode == "strict":
        if ca_bundle is None:
            msg = "UNIFI_CA_BUNDLE is required when UNIFI_TLS_MODE=strict"
            raise UniFiUnavailableError(msg)
        ctx.load_verify_locations(cafile=ca_bundle)
    elif mode == "extract-once":
        if extracted_pem is None:
            msg = "extract-once bootstrap did not produce a certificate"
            raise UniFiUnavailableError(msg)
        ctx.load_verify_locations(cadata=extracted_pem)
    else:
        msg = f"unknown UNIFI_TLS_MODE: {mode!r}"
        raise ValueError(msg)
    ctx.check_hostname = False
    logger.warning(
        "TLS hostname verification disabled: gateway certificate has no SAN "
        "for the base-URL host; chain verification against the CA bundle "
        "remains active"
    )
    return ctx
