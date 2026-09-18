"""Bounded ASGI request transport for the existing application controller.

No socket-backed BaseHTTPRequestHandler is instantiated. A private controller
is created for every request. Blocking domain work runs in the framework thread
pool; background native launches keep the application's durable run ownership.
"""
from __future__ import annotations

from email.message import Message
import io
import json
import logging
import math
import re
from urllib.parse import urlparse

import anyio
from fastapi import Request
from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, Response

import server
from api_models import JsonObject

MAX_BODY_BYTES = 1024 * 1024
BODY_TIMEOUT_SECONDS = 30
_SINGLE_HEADERS = {
    "authorization", "cookie", "x-opu-actor", "x-csrf-token", "origin", "host",
    "content-length", "content-type", "transfer-encoding", "content-encoding",
}
_logger = logging.getLogger("opu.api")


class InvalidRequest(ValueError):
    pass


class RequestBodyTimeout(Exception):
    pass


def error(status: int, code: str, message: str) -> Response:
    return JSONResponse({"error": code, "message": message}, status_code=status,
                        headers={"Cache-Control": "no-store"})


def _headers(request: Request) -> Message:
    values = Message()
    seen = set()
    headers = request.scope.get("headers", [])
    if len(headers) > 100 or sum(len(k) + len(v) for k, v in headers) > 32768:
        raise InvalidRequest("Too many or oversized request headers")
    for raw_name, raw_value in headers:
        name, value = raw_name.decode("ascii").lower(), raw_value.decode("latin-1")
        if re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-z-]+", name) is None:
            raise InvalidRequest("Invalid header name")
        if name in _SINGLE_HEADERS and name in seen:
            raise InvalidRequest("Duplicate security or framing header")
        if any(char in value for char in ("\r", "\n", "\x00")):
            raise InvalidRequest("Invalid header value")
        seen.add(name)
        values[name] = value
    if values.get("Transfer-Encoding") is not None:
        raise InvalidRequest("Transfer-Encoding is not supported")
    if values.get("Content-Encoding", "identity").lower() != "identity":
        raise InvalidRequest("Encoded request bodies are not supported")
    return values


def _target(request: Request) -> str:
    # Preserve the existing controller's raw URL semantics; ASGI path is decoded
    # already, and a second decoding could change identifiers or access policy.
    raw = request.scope.get("raw_path")
    path = raw.decode("ascii") if raw is not None else request.scope["path"]
    query = request.scope.get("query_string", b"").decode("ascii")
    if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
        raise InvalidRequest("Invalid request target")
    target = path + ("?" + query if query else "")
    if any(ord(char) < 32 or ord(char) == 127 for char in target):
        raise InvalidRequest("Invalid request target")
    return target


class AsgiController(server.Controller):
    def __init__(self, request: Request):
        self.path = _target(request)
        self.command = request.method
        self.headers = _headers(request)
        self.client_address = request.client.host if request.client else "unknown"
        self.body = {}
        self.wfile = io.BytesIO()
        self.response_status: int | None = None
        self.response_headers: list[tuple[bytes, bytes]] = []
        self.headers_finished = False

    def address_string(self):
        return self.client_address

    def _require_api_auth(self) -> bool:
        if not super()._require_api_auth():
            return False
        identity = (getattr(self, "_principal", None), bool(getattr(self, "_company_session", None)))
        if hasattr(self, "_transport_identity") and identity != self._transport_identity:
            self._send_json(403, {"error": "unauthorized", "message": "Authenticated identity changed before dispatch"})
            return False
        self._transport_identity = identity
        return True

    def _read_json_body(self) -> dict:
        # Already bounded and strictly parsed after initial authorization. The
        # controller still performs fresh identity/role checks around this read.
        return getattr(self, "_parsed_body", self.body)

    def send_response(self, status, message=None):
        if self.response_status is not None:
            raise RuntimeError("Controller attempted multiple HTTP responses")
        self.response_status = int(status)

    def send_header(self, name, value):
        if self.headers_finished or any(c in name + str(value) for c in ("\r", "\n", "\x00")):
            raise RuntimeError("Invalid response header")
        self.response_headers.append((name.lower().encode("ascii"), str(value).encode("latin-1")))

    def end_headers(self):
        self.headers_finished = True

    def send_error(self, status, message=None, explain=None):
        self._send_json(status, {"error": "not_found" if status == 404 else "request_failed",
                                 "message": "Unknown route" if status == 404 else "Request failed"})

    def admit(self) -> bool:
        path = urlparse(self.path).path
        if (not path.startswith("/api/") or self.command == "GET" and path in {"/api/health", "/api/auth/config"}
                or self.command == "POST" and path == "/api/auth/logout"):
            return True
        return self._require_api_auth() and self._authorize_path(self.command, path)

    def response(self, model: type[BaseModel] | None = None) -> Response:
        if self.response_status is None or not self.headers_finished:
            raise RuntimeError("Controller did not finish its HTTP response")
        body = self.wfile.getvalue()
        if model is not None and self.response_status == 200:
            # Validation is explicit because a transport Response must preserve
            # error status, duplicate Set-Cookie headers and no-store behavior.
            value = model.model_validate(json.loads(body), strict=True)
            body = json.dumps(value.model_dump(mode="json", exclude_unset=True), allow_nan=False).encode()
        response = Response(body, status_code=self.response_status)
        response.raw_headers = [(k, v) for k, v in self.response_headers if k != b"content-length"]
        if self.response_status not in {204, 304}:
            response.raw_headers.append((b"content-length", str(len(body)).encode("ascii")))
        return response


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidRequest("Duplicate JSON object field")
        result[key] = value
    return result


def _nonfinite(_value):
    raise InvalidRequest("Non-finite JSON numbers are not supported")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise InvalidRequest("JSON number is outside the supported finite range")
    return number


async def read_body(request: Request, controller: AsgiController) -> dict:
    value = controller.headers.get("Content-Length")
    if value is not None and not re.fullmatch(r"[0-9]+", value):
        raise InvalidRequest("Content-Length must be a nonnegative integer")
    # Avoid converting an unbounded decimal string, including Python's int limit.
    if value is not None and (len(value) > 10 or int(value) > MAX_BODY_BYTES):
        raise InvalidRequest("JSON body must be at most 1 MiB")
    expected = int(value or "0")
    if request.method == "GET" and expected:
        raise InvalidRequest("GET request bodies are not supported")
    raw = bytearray()
    try:
        with anyio.fail_after(BODY_TIMEOUT_SECONDS):
            async for chunk in request.stream():
                if len(raw) + len(chunk) > MAX_BODY_BYTES or len(raw) + len(chunk) > expected:
                    raise InvalidRequest("Request body exceeds its declared length or 1 MiB limit")
                raw.extend(chunk)
    except TimeoutError as exc:
        raise RequestBodyTimeout() from exc
    if len(raw) != expected:
        raise InvalidRequest("Incomplete request body")
    if not raw:
        return {}
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=_nonfinite, parse_float=_finite_float)
        return JsonObject.model_validate(value, strict=True).root
    except (ValueError, UnicodeError, RecursionError) as exc:
        # Never copy Pydantic input values or JSON data into an error response.
        raise InvalidRequest("Request body must be a valid UTF-8 JSON object with unique fields") from exc


async def dispatch(request: Request, model: type[BaseModel] | None = None, *, schema=None) -> Response:
    try:
        controller = AsgiController(request)
        if not await run_in_threadpool(controller.admit):
            return controller.response()
        controller.body = await read_body(request, controller)
        if schema is not None:
            # Schema retrieval uses the same bounded parser and fresh identity
            # checks as other protected reads, including after a delayed body.
            if not await run_in_threadpool(controller.admit):
                return controller.response()
            return JSONResponse(schema(), headers={"Cache-Control": "no-store"})
        action = controller.do_GET if request.method == "GET" else controller.do_POST
        await run_in_threadpool(action)
        return controller.response(model)
    except (InvalidRequest, UnicodeError, ClientDisconnect):
        return error(400, "invalid_request", "Malformed, ambiguous or oversized request")
    except RequestBodyTimeout:
        return error(408, "request_timeout", "Request body was not completed in time")
    except Exception as exc:
        # Auth/configuration errors raised outside the old dispatch wrapper keep
        # their safe, explicit envelopes. Unexpected details stay off the wire.
        if isinstance(exc, server.auth.AuthError):
            return error(exc.status, exc.error, exc.message)
        if isinstance(exc, server.host_config.HostConfigError):
            return error(503, "invalid_host_config", str(exc))
        _logger.error("Request processing failed (%s)", type(exc).__name__)
        return error(500, "invalid_response" if isinstance(exc, ValidationError) else "internal_error",
                     "Request could not be completed; inspect the controller evidence before retrying")
