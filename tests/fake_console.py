"""A small fake UniFi console, so the real HTTP client can be tested end to end."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer

from .helpers import CONSOLE_JSON, PASSWORD, USERNAME, profile_json


def _jwt(claims: dict[str, Any]) -> str:
    def segment(obj: dict[str, Any]) -> str:
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{segment({'alg': 'HS256', 'typ': 'JWT'})}.{segment(claims)}.signature"


class FakeConsole:
    """In-memory UniFi OS login, Protect nvr/websocket and Alarm Manager API."""

    def __init__(self) -> None:
        self.username = USERNAME
        self.password = PASSWORD
        self.super_admin = True
        self.login_status: int | None = None  # force a login status, e.g. 429 or 499
        self.send_csrf_header = True
        self.jwt_tokens = False
        self.updated_csrf_on_get: str | None = None
        self.profiles: list[dict[str, Any]] = [profile_json()]
        self.profiles_payload: Any = None  # replaces the GET /profiles body
        self.nvr: dict[str, Any] = json.loads(json.dumps(CONSOLE_JSON))
        self.login_count = 0
        self.ws_connections = 0
        self.ws_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._sessions: dict[str, str] = {}  # TOKEN cookie -> expected CSRF token
        self._open_sockets: set[web.WebSocketResponse] = set()
        self.server: TestServer | None = None

    @property
    def host(self) -> str:
        """host:port of the running server."""
        assert self.server is not None
        return f"127.0.0.1:{self.server.port}"

    async def start(self) -> None:
        """Start listening on 127.0.0.1."""
        app = web.Application()
        app.router.add_post("/api/auth/login", self._login)
        app.router.add_get("/proxy/protect/api/nvr", self._nvr)
        app.router.add_get("/api/v2/alarms/profiles", self._get_profiles)
        app.router.add_post(
            "/api/v2/alarms/profiles/{profile_id}/actions/{verb}", self._action
        )
        app.router.add_get("/proxy/protect/ws/updates", self._ws)
        self.server = TestServer(app, host="127.0.0.1")
        await self.server.start_server()

    async def stop(self) -> None:
        """Close open websockets, then the server."""
        for ws in list(self._open_sockets):
            await ws.close()
        if self.server is not None:
            await self.server.close()

    def expire_sessions(self) -> None:
        """Invalidate every TOKEN cookie, as a console reboot would."""
        self._sessions.clear()

    def rotate_csrf(self) -> None:
        """Make every session expect a CSRF token the client doesn't have."""
        for token in self._sessions:
            self._sessions[token] = "rotated"

    def _token(self, request: web.Request) -> str | None:
        token = request.cookies.get("TOKEN")
        return token if token in self._sessions else None

    async def _login(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.login_count += 1
        if self.login_status is not None:
            return web.json_response({"code": "FORCED"}, status=self.login_status)
        if (
            body.get("username") != self.username
            or body.get("password") != self.password
        ):
            return web.json_response(
                {"error": {"code": 403, "message": "Forbidden"}}, status=403
            )
        csrf = f"csrf-{self.login_count}"
        token = (
            _jwt({"csrfToken": csrf})
            if self.jwt_tokens
            else f"token-{self.login_count}"
        )
        self._sessions[token] = csrf
        headers = {"X-CSRF-Token": csrf} if self.send_csrf_header else {}
        response = web.json_response({"unique_id": "fake-user"}, headers=headers)
        response.set_cookie("TOKEN", token)
        return response

    async def _nvr(self, request: web.Request) -> web.Response:
        if self._token(request) is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response(self.nvr)

    async def _get_profiles(self, request: web.Request) -> web.Response:
        token = self._token(request)
        if token is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        if not self.super_admin:
            return web.Response(text="Forbidden", status=403)
        headers = {}
        if self.updated_csrf_on_get:
            self._sessions[token] = self.updated_csrf_on_get
            headers["X-Updated-CSRF-Token"] = self.updated_csrf_on_get
        payload = (
            self.profiles if self.profiles_payload is None else self.profiles_payload
        )
        return web.json_response(payload, headers=headers)

    async def _action(self, request: web.Request) -> web.Response:
        token = self._token(request)
        if token is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        if request.headers.get("X-CSRF-Token") != self._sessions[token]:
            return web.Response(text="Forbidden", status=403)
        if not self.super_admin:
            return web.Response(text="Forbidden", status=403)
        profile_id = request.match_info["profile_id"]
        verb = request.match_info["verb"]
        for profile in self.profiles:
            if profile["id"] == profile_id:
                profile["state"] = "arming" if verb == "arm" else "disarmed"
                return web.json_response(profile)
        return web.json_response({"error": "not found"}, status=404)

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        if self._token(request) is None:
            raise web.HTTPUnauthorized
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ws_connections += 1
        self._open_sockets.add(ws)
        pump = asyncio.create_task(self._pump(ws))
        try:
            async for _message in ws:
                pass
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
            self._open_sockets.discard(ws)
        return ws

    async def _pump(self, ws: web.WebSocketResponse) -> None:
        while True:
            item = await self.ws_queue.get()
            if item is None:
                await ws.close()
                return
            await ws.send_bytes(item)
