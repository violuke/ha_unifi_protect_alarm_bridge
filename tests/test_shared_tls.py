"""Verify a wrong certificate is rejected before fake credentials are sent."""

import asyncio
from datetime import UTC, datetime, timedelta
import ssl
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from homeassistant.config_entries import ConfigEntryState
import pytest

from custom_components.unifi_protect_alarm_bridge.api import (
    CannotConnect,
)
from custom_components.unifi_protect_alarm_bridge.shared import SharedProtectClient

from .helpers import AWAY_ID, CONSOLE_JSON, profile_json


async def test_shared_certificate_checks_before_authenticated_requests(
    socket_enabled, tmp_path
):
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()

    def expected_rejected_handshake(event_loop, details):
        # Our client intentionally aborts the fake server's TLS handshake.
        if (
            isinstance(details.get("exception"), ConnectionResetError)
            and details.get("message")
            == "Error on transport creation for incoming connection"
        ):
            return
        if previous_handler:
            previous_handler(event_loop, details)
        else:
            event_loop.default_exception_handler(details)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fake-console")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "fake-cert.pem", tmp_path / "fake-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    received_logins = []
    received_status_reads = []
    received_actions = []

    async def login(request):
        received_logins.append(await request.json())
        return web.json_response({}, headers={"X-CSRF-Token": "fake-csrf"})

    async def status(request):
        received_status_reads.append(request.headers.get("X-CSRF-Token"))
        return web.json_response(CONSOLE_JSON)

    async def profiles(request):
        return web.json_response([profile_json()])

    async def action(request):
        received_actions.append(
            (request.match_info["action"], request.headers.get("X-CSRF-Token"))
        )
        return web.json_response(
            profile_json(
                state="armed" if request.match_info["action"] == "arm" else "disarmed"
            )
        )

    app = web.Application()
    app.router.add_post("/api/auth/login", login)
    app.router.add_get("/proxy/protect/api/nvr", status)
    app.router.add_get("/api/v2/alarms/profiles", profiles)
    app.router.add_post("/api/v2/alarms/profiles/{profile}/actions/{action}", action)
    server = TestServer(app, host="127.0.0.1", scheme="https")
    await server.start_server(ssl=context)
    loop.set_exception_handler(expected_rejected_handshake)
    try:
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=False)
        ) as session:
            host = f"127.0.0.1:{server.port}"
            native_api = SimpleNamespace(
                base_url=f"https://{host}",
                is_public_only=False,
                is_authenticated=lambda: True,
                get_session=AsyncMock(return_value=session),
                headers={"X-CSRF-Token": "fake-existing-session"},
            )
            source = SimpleNamespace(
                domain="unifiprotect",
                state=ConfigEntryState.LOADED,
                runtime_data=SimpleNamespace(api=native_api),
            )
            fake_hass = SimpleNamespace(
                config_entries=SimpleNamespace(async_get_entry=lambda _id: source)
            )
            for fingerprint in (None, "ab" * 32):
                borrowed = SharedProtectClient(fake_hass, "fake-source", fingerprint)
                with pytest.raises(CannotConnect):
                    await borrowed.async_get_console_info()
                assert received_status_reads == []
            borrowed = SharedProtectClient(
                fake_hass, "fake-source", cert.fingerprint(hashes.SHA256()).hex()
            )
            assert (await borrowed.async_get_console_info()).mac == CONSOLE_JSON["mac"]
            assert received_status_reads == ["fake-existing-session"]
            await borrowed.async_get_profiles()
            for action_name, expected_state in (
                ("arm", "armed"),
                ("disarm", "disarmed"),
            ):
                assert (
                    await getattr(borrowed, f"async_{action_name}")(AWAY_ID)
                ).state == expected_state
            assert received_actions == [
                ("arm", "fake-existing-session"),
                ("disarm", "fake-existing-session"),
            ]
            # Even an already discovered profile cannot bypass TLS validation.
            borrowed.ssl = aiohttp.Fingerprint(bytes.fromhex("ab" * 32))
            with pytest.raises(CannotConnect):
                await borrowed.async_arm(AWAY_ID)
            assert len(received_actions) == 2
            assert received_logins == []
    finally:
        await server.close()
        loop.set_exception_handler(previous_handler)
