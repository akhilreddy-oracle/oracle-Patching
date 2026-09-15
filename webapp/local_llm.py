"""Bounded OpenAI-compatible chat transport to an administrator-selected local LLM.

This client returns text and proposed function calls. It never executes tools,
selects endpoints from prompts, downloads models or falls back to a cloud API.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import socket
import ssl
import stat
import threading
import time
from urllib.parse import urlsplit

import runtime_paths

CONFIG_ENV = "OPU_ASSISTANT_CONFIG"
MAX_REQUEST = 512 * 1024
MAX_RESPONSE = 1024 * 1024
MAX_MESSAGES = 64
MAX_TOOLS = 16
MAX_TOOL_CALLS = 8
MAX_ARGUMENTS = 16384
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}\Z")
_CALL_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_PRIVATE = tuple(ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"))


class LLMError(Exception):
    def __init__(self, message, status=503):
        super().__init__(message)
        self.message, self.status = message, status

    def to_json(self):
        return {"error": "local_llm_error", "message": self.message}


def _require(condition, message, status=503):
    if not condition:
        raise LLMError(message, status)


def _json(raw, message, status):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    def invalid(_value):
        raise ValueError("nonfinite value")
    try:
        return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)
    except (ValueError, UnicodeError, RecursionError):
        raise LLMError(message, status) from None


def _encode(value, message, status=400):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise LLMError(message, status) from None


def _internal(address):
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return False
    return ip.is_loopback or any(ip.version == network.version and ip in network for network in _PRIVATE)


def _endpoint(c):
    url = c.get("base_url", "http://127.0.0.1:11434/v1")
    _require(isinstance(url, str) and 0 < len(url) <= 2048 and not re.search(r"[\s%\\]", url), "Local model endpoint is invalid")
    try:
        parsed = urlsplit(url)
        host, port = parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise LLMError("Local model endpoint is invalid") from None
    _require(parsed.scheme in {"http", "https"} and host and not parsed.username and not parsed.password
             and not parsed.query and not parsed.fragment and 1 <= port <= 65535, "Local model endpoint is invalid")
    _require(re.fullmatch(r"[A-Za-z0-9._~/-]*", parsed.path) and not any(part in {".", ".."} for part in parsed.path.split("/")), "Local model endpoint path is invalid")
    prefix = parsed.path.rstrip("/")
    _require(prefix.endswith("/v1"), "Local model base_url must end in /v1")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
        _require(re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", host), "Local model hostname is invalid")
    loopback = host == "localhost" or bool(ip and ip.is_loopback)
    _require(loopback or c.get("allow_private_endpoint") is True, "An internal model endpoint requires explicit allow_private_endpoint")
    _require(ip is None or _internal(str(ip)), "Public, link-local and metadata model endpoints are not allowed")
    _require(loopback or parsed.scheme == "https" or c.get("allow_insecure_private") is True, "Internal model endpoints require HTTPS")
    return {"host": host, "port": port, "https": parsed.scheme == "https", "path": prefix + "/chat/completions"}


def _load_config():
    try:
        configured_path = os.environ.get(CONFIG_ENV)
        path = Path(configured_path) if configured_path else runtime_paths.state_dir() / "assistant-config.json"
        _require(path.is_absolute(), "Local assistant configuration path must be absolute")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_uid in {0, os.geteuid()}
                     and not before.st_mode & 0o022, "Local assistant configuration is not administrator-protected")
            raw = stream.read(65537)
            after = os.fstat(stream.fileno())
            _require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                     == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns), "Local assistant configuration changed while reading")
        _require(len(raw) <= 65536, "Local assistant configuration is too large")
    except FileNotFoundError:
        raise LLMError("Local assistant is not configured") from None
    except (OSError, ValueError):
        raise LLMError("Local assistant configuration is unavailable or invalid") from None
    c = _json(raw, "Local assistant configuration is invalid JSON", 503)
    allowed = {"enabled", "provider", "base_url", "model", "timeout_seconds", "max_tokens", "api_key_env", "allow_private_endpoint", "allow_insecure_private"}
    _require(isinstance(c, dict) and not set(c) - allowed, "Local assistant configuration has unsupported fields")
    _require(type(c.get("enabled")) is bool, "Local assistant enabled must be an explicit boolean")
    for field in ("allow_private_endpoint", "allow_insecure_private"):
        _require(field not in c or type(c[field]) is bool, "Local assistant endpoint permissions must be booleans")
    provider = c.get("provider", "ollama")
    _require(isinstance(provider, str) and provider in {"ollama", "openai-compatible"}, "Unsupported local model provider")
    c["provider"] = provider
    if not c["enabled"]:
        return {"enabled": False, "provider": provider, "model": None}
    model = c.get("model")
    _require(isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,127}", model), "Local assistant model is required and must be a valid model identifier")
    _require(not re.search(r"(?:^|[:/-])cloud(?:$|[:/-])", model, re.IGNORECASE), "Cloud model identifiers are not allowed")
    c["endpoint"] = _endpoint(c)
    for field, default, lower, upper in (("timeout_seconds", 60, 5, 120), ("max_tokens", 2048, 64, 8192)):
        c.setdefault(field, default)
        _require(type(c[field]) is int and lower <= c[field] <= upper, f"Local assistant {field} must be between {lower} and {upper}")
    key_env = c.get("api_key_env")
    _require(key_env is None or isinstance(key_env, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", key_env), "Local assistant api_key_env is invalid")
    return c


def config_status():
    """Read configuration without connecting or looking up an API-key value."""
    try:
        c = _load_config()
        return {"enabled": c["enabled"], "configured": True, "model": c["model"], "provider": c["provider"],
                "reason": "Local model endpoint configured; connectivity and model availability have not been checked" if c["enabled"] else "Local assistant is disabled"}
    except LLMError as exc:
        return {"enabled": False, "configured": False, "model": None, "provider": None, "reason": exc.message}


def load_config():
    """Return validated effective settings without reading a credential value.

    Intended for administrator-side diagnostics. Do not expose the returned
    endpoint or credential environment-variable name through user-facing APIs.
    """
    return _load_config()


def _tool_calls(calls, names, *, status):
    _require(isinstance(calls, list) and len(calls) <= MAX_TOOL_CALLS, "Local model tool calls are invalid or exceed the limit", status)
    result, seen = [], set()
    for call in calls:
        _require(isinstance(call, dict), "Local model tool call is malformed", status)
        identifier, function = call.get("id"), call.get("function")
        _require(isinstance(identifier, str) and _CALL_ID.fullmatch(identifier) and identifier not in seen
                 and call.get("type") == "function" and isinstance(function, dict), "Local model tool call identity is invalid", status)
        name, arguments = function.get("name"), function.get("arguments")
        _require(isinstance(name, str) and name in names, "Local model requested an unavailable tool", status)
        _require(isinstance(arguments, str) and len(arguments.encode("utf-8", errors="replace")) <= MAX_ARGUMENTS, "Local model tool arguments are invalid or too large", status)
        parsed = _json(arguments, "Local model tool arguments are invalid JSON", status)
        _require(isinstance(parsed, dict), "Local model tool arguments must be a JSON object", status)
        result.append({"id": identifier, "type": "function", "function": {"name": name, "arguments": arguments}})
        seen.add(identifier)
    return result


def _payload(messages, tools, c):
    _require(isinstance(tools, list) and len(tools) <= MAX_TOOLS, "Too many or invalid assistant tools", 400)
    names, definitions = set(), []
    for tool in tools:
        _require(isinstance(tool, dict) and set(tool) == {"type", "function"} and tool["type"] == "function" and isinstance(tool["function"], dict), "Assistant tool definition is invalid", 400)
        function = tool["function"]
        name, description, parameters = function.get("name"), function.get("description", ""), function.get("parameters")
        _require(not set(function) - {"name", "description", "parameters", "strict"} and isinstance(name, str) and _NAME.fullmatch(name)
                 and name not in names and isinstance(description, str) and len(description) <= 8192
                 and isinstance(parameters, dict) and parameters.get("type") == "object"
                 and ("strict" not in function or type(function["strict"]) is bool), "Assistant tool definition is invalid", 400)
        names.add(name); definitions.append(tool)
    _require(isinstance(messages, list) and 1 <= len(messages) <= MAX_MESSAGES, "Assistant conversation must contain 1–64 messages", 400)
    history = []
    for message in messages:
        _require(isinstance(message, dict) and not set(message) - {"role", "content", "tool_calls", "tool_call_id", "name"}, "Assistant message contains unsupported fields", 400)
        role, content = message.get("role"), message.get("content")
        _require(isinstance(role, str) and role in {"system", "user", "assistant", "tool"} and (isinstance(content, str) or role == "assistant" and content is None), "Assistant message role or content is invalid", 400)
        _require(content is None or len(content) <= 131072, "Assistant message is too large", 400)
        normalized = {"role": role, "content": content}
        if "name" in message:
            _require(isinstance(message["name"], str) and _NAME.fullmatch(message["name"]), "Assistant message name is invalid", 400)
            normalized["name"] = message["name"]
        if "tool_calls" in message:
            _require(role == "assistant", "Only assistant messages can propose tool calls", 400)
            normalized["tool_calls"] = _tool_calls(message["tool_calls"], names, status=400)
        if role == "tool":
            identifier = message.get("tool_call_id")
            _require(isinstance(identifier, str) and _CALL_ID.fullmatch(identifier), "Tool response identifier is required", 400)
            normalized["tool_call_id"] = identifier
        else:
            _require("tool_call_id" not in message, "Unexpected tool response identifier", 400)
        history.append(normalized)
    payload = {"model": c["model"], "messages": history, "stream": False, "max_tokens": c["max_tokens"], "temperature": 0}
    if definitions:
        payload.update(tools=definitions, tool_choice="auto")
    raw = _encode(payload, "Assistant request cannot be encoded")
    _require(len(raw) <= MAX_REQUEST, "Assistant request exceeds the size limit", 400)
    return raw, names


def _addresses(endpoint, timeout):
    # Resolve once, validate every answer, then connect directly to the checked
    # socket address. HTTPS retains the configured hostname for SNI/cert checks.
    host = "127.0.0.1" if endpoint["host"] == "localhost" else endpoint["host"]
    answers = queue.Queue(maxsize=1)
    def resolve():
        try:
            answers.put(socket.getaddrinfo(host, endpoint["port"], type=socket.SOCK_STREAM))
        except OSError:
            answers.put(None)
    threading.Thread(target=resolve, daemon=True, name="local-llm-resolver").start()
    try:
        addresses = answers.get(timeout=timeout)
        _require(addresses is not None, "Local model endpoint could not be resolved safely")
        _require(addresses and len(addresses) <= 16 and all(_internal(item[4][0]) for item in addresses), "Model endpoint DNS must resolve only to loopback or private internal addresses")
        return addresses[0]
    except queue.Empty:
        raise LLMError("Local model endpoint resolution timed out", 504) from None
    except (OSError, ValueError):
        raise LLMError("Local model endpoint could not be resolved safely") from None


class _PinnedConnection(http.client.HTTPConnection):
    def __init__(self, endpoint, address, timeout):
        super().__init__(endpoint["host"], endpoint["port"], timeout=timeout)
        self.endpoint, self.address = endpoint, address
        self.transport_socket = None

    def connect(self):
        family, kind, protocol, _name, sockaddr = self.address
        channel = socket.socket(family, kind, protocol)
        self.sock = channel
        self.transport_socket = channel
        try:
            channel.settimeout(self.timeout)
            channel.connect(sockaddr)
            self.sock = ssl.create_default_context().wrap_socket(channel, server_hostname=self.host) if self.endpoint["https"] else channel
            self.transport_socket = self.sock
        except BaseException:
            channel.close()
            raise


def _exchange(c, raw):
    endpoint = c["endpoint"]
    headers = {"Content-Type": "application/json", "Accept": "application/json", "Accept-Encoding": "identity"}
    if c.get("api_key_env"):
        key = os.environ.get(c["api_key_env"])
        _require(isinstance(key, str) and 0 < len(key) <= 4096 and all(33 <= ord(character) <= 126 for character in key), "Configured local model credential is unavailable or invalid")
        headers["Authorization"] = "Bearer " + key
    deadline = time.monotonic() + c["timeout_seconds"]
    address = _addresses(endpoint, c["timeout_seconds"])
    remaining = deadline - time.monotonic()
    _require(remaining > 0, "Local model request timed out", 504)
    connection = _PinnedConnection(endpoint, address, remaining)
    expired = threading.Event()
    def expire():
        expired.set()
        channel = connection.transport_socket
        if channel is not None:
            try:
                channel.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        connection.close()
    timer = threading.Timer(remaining, expire)
    timer.daemon = True
    timer.start()
    try:
        connection.request("POST", endpoint["path"], body=raw, headers=headers)
        response = connection.getresponse()
        _require(not 300 <= response.status < 400, "Local model endpoint redirects are not allowed", 502)
        _require(response.status == 200, f"Local model server returned HTTP {response.status}; check its availability and configured model", 502)
        _require(response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() == "application/json", "Local model returned an unsupported response type", 502)
        _require(response.getheader("Content-Encoding", "identity").lower() in {"", "identity"}, "Compressed local model responses are not supported", 502)
        length = response.getheader("Content-Length")
        _require(length is None or length.isdigit() and int(length) <= MAX_RESPONSE, "Local model response exceeds the size limit", 502)
        chunks, total = [], 0
        while True:
            remaining = deadline - time.monotonic()
            _require(remaining > 0, "Local model request timed out", 504)
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(65536, MAX_RESPONSE + 1 - total))
            if not chunk:
                break
            chunks.append(chunk); total += len(chunk)
            _require(total <= MAX_RESPONSE, "Local model response exceeds the size limit", 502)
        _require(length is None or total == int(length), "Local model response was incomplete", 502)
        return _json(b"".join(chunks), "Local model returned invalid JSON", 502)
    except (TimeoutError, socket.timeout):
        raise LLMError("Local model request timed out", 504) from None
    except LLMError:
        if expired.is_set():
            raise LLMError("Local model request timed out", 504) from None
        raise
    except (OSError, http.client.HTTPException, ValueError):
        if expired.is_set():
            raise LLMError("Local model request timed out", 504) from None
        raise LLMError("Local model server is unavailable or returned an invalid response", 503) from None
    finally:
        timer.cancel()
        connection.close()


def complete(messages, tools, *, expected_config=None):
    c = _load_config()
    _require(expected_config is None or c == expected_config,
             "Local assistant configuration changed before the request")
    _require(c["enabled"], "Local assistant is disabled")
    raw, names = _payload(messages, tools, c)
    response = _exchange(c, raw)
    choices = response.get("choices") if isinstance(response, dict) else None
    _require(isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict), "Local model response must contain one completion", 502)
    choice = choices[0]
    _require(isinstance(choice.get("finish_reason"), str) and choice["finish_reason"] in {"stop", "tool_calls"}, "Local model response was incomplete or unsupported", 502)
    message = choice.get("message")
    _require(isinstance(message, dict) and message.get("role") == "assistant", "Local model returned an invalid assistant message", 502)
    content = message.get("content")
    _require(content is None or isinstance(content, str) and len(content) <= 131072, "Local model content is invalid or too large", 502)
    calls = _tool_calls(message["tool_calls"] if message.get("tool_calls") is not None else [], names, status=502)
    _require(bool(calls) or isinstance(content, str) and bool(content.strip()), "Local model returned an empty response", 502)
    return {"role": "assistant", "content": content, "tool_calls": calls}
