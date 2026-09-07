# Copyright (c) 2026 Rackslab
#
# This file is part of Slurm-web.
#
# SPDX-License-Identifier: MIT

"""Shared helpers for the multiplexed job live WebSocket session.

One WebSocket per job carries several channels (checks, log, gpu, job). Every
channel has its own resume cursor so a reconnecting client never loses or
duplicates data. The same JSON message shapes are used between the browser and
the gateway, and between the gateway and the agent.
"""

import json
import logging

from flask import Response, abort, current_app, request
from rfl.authentication.errors import JWTDecodeError

try:
    from simple_websocket import Client, ConnectionClosed, Server
except ImportError:  # pragma: no cover - exercised only on hosts without the dep
    Client = None
    Server = None

    class ConnectionClosed(Exception):
        pass


logger = logging.getLogger(__name__)

# Application close codes (4000-4999 are reserved for applications).
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_NOT_FOUND = 4404
CLOSE_BAD_REQUEST = 4400
CLOSE_UPSTREAM = 4502
CLOSE_INTERNAL = 1011

LIVE_CHANNELS = ("checks", "log", "gpu", "job")
PING_INTERVAL = 25
MAX_MESSAGE_SIZE = 1 << 20
AUTH_TIMEOUT = 10


class LiveSessionError(Exception):
    """Protocol error that must close the WebSocket with an application code."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class WebSocketResponse(Response):
    """Empty WSGI response returned after a WebSocket handler completes.

    The socket was hijacked by simple-websocket, so the WSGI server must not
    write anything on it. This mirrors the approach used by Flask-Sock.
    """

    def __init__(self, ws):
        super().__init__()
        self.ws = ws

    def __call__(self, environ, start_response):
        mode = getattr(self.ws, "mode", None)
        if mode == "gunicorn":
            raise StopIteration()
        if mode == "werkzeug":
            raise ConnectionError()
        return []


def websocket_available() -> bool:
    return Server is not None


def accept_websocket():
    """Complete the WebSocket handshake for the current Flask request."""
    if Server is None:
        abort(501, "WebSocket support requires the simple-websocket package")
    if request.headers.get("Upgrade", "").lower() != "websocket":
        abort(426, "This endpoint requires a WebSocket upgrade")
    return Server.accept(
        request.environ, ping_interval=PING_INTERVAL, max_message_size=MAX_MESSAGE_SIZE
    )


def connect_websocket(url: str, token: str):
    """Open a client WebSocket to an upstream agent with bearer authentication."""
    if Client is None:
        raise LiveSessionError(CLOSE_INTERNAL, "simple-websocket is not installed")
    if url.startswith("https://"):
        url = "wss://" + url[len("https://") :]
    elif url.startswith("http://"):
        url = "ws://" + url[len("http://") :]
    return Client.connect(
        url,
        headers={"Authorization": f"Bearer {token}"},
        ping_interval=PING_INTERVAL,
        max_message_size=MAX_MESSAGE_SIZE,
    )


def send_json(ws, payload: dict) -> None:
    ws.send(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))


def receive_json(ws, timeout):
    """Return the next JSON object from the socket or None on timeout.

    Non-JSON or non-object frames raise LiveSessionError so the caller can close
    the connection with a protocol error.
    """
    raw = ws.receive(timeout=timeout)
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        message = json.loads(raw)
    except ValueError:
        raise LiveSessionError(CLOSE_BAD_REQUEST, "messages must be JSON objects")
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        raise LiveSessionError(CLOSE_BAD_REQUEST, "messages must carry a string type")
    return message


def close_websocket(ws, code: int = 1000, message: str = None) -> None:
    try:
        ws.close(code, message)
    except (ConnectionClosed, OSError):
        pass


def authenticate_websocket(ws):
    """Return (token, user) for a freshly accepted WebSocket.

    Server-to-server hops (gateway to agent) send the bearer token in the HTTP
    Authorization header. Browsers cannot set that header, so they send a first
    frame ``{"type": "auth", "token": "..."}`` instead. The token never appears
    in the URL, so it does not leak into access logs or proxies.
    """
    token = None
    auth = request.headers.get("Authorization")
    if auth:
        if not auth.startswith("Bearer "):
            raise LiveSessionError(CLOSE_UNAUTHORIZED, "Malformed authorization header")
        token = auth.split(" ", 1)[1]
    else:
        message = receive_json(ws, AUTH_TIMEOUT)
        if message is None or message.get("type") != "auth":
            raise LiveSessionError(CLOSE_UNAUTHORIZED, "First message must be auth")
        token = message.get("token")
        if not isinstance(token, str) or not token:
            raise LiveSessionError(CLOSE_UNAUTHORIZED, "auth message requires a token")
    try:
        user = current_app.jwt.decode(token)
    except JWTDecodeError as err:
        raise LiveSessionError(CLOSE_UNAUTHORIZED, str(err))
    # Mirror rfl.web.tokens.check_jwt so downstream view helpers keep working.
    request.token = token
    request.user = user
    return token, user


def finish_websocket(ws, error: LiveSessionError = None):
    """Send an optional error frame, close the socket and build the WSGI response."""
    if error is not None:
        try:
            send_json(ws, {"type": "error", "code": error.code, "message": error.message})
        except (ConnectionClosed, OSError):
            pass
        close_websocket(ws, error.code, error.message[:120])
    else:
        close_websocket(ws)
    return WebSocketResponse(ws)
