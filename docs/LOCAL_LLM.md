# Local assistant model connection

The assistant uses a standard-library OpenAI-compatible chat client. Its default
endpoint is Ollama on the controller's own server,
`http://127.0.0.1:11434/v1`. It does not download models or fall back to a cloud
service. Select an already installed model that supports function calling.

An administrator enables the connection in `assistant-config.json` under the
controller runtime state directory (`OPU_WEBAPP_STATE_DIR`, default `webapp/var`).
Alternatively, `OPU_ASSISTANT_CONFIG` can name an absolute configuration path.
The file must be a regular, single-link file owned by the controller or root,
without group/world write access. Use mode `0600`.

```json
{
  "enabled": true,
  "provider": "ollama",
  "base_url": "http://127.0.0.1:11434/v1",
  "model": "your-installed-local-model",
  "timeout_seconds": 60,
  "max_tokens": 2048
}
```

Setting `enabled` to `false` disables inference. A missing or invalid
configuration also prevents inference. Chat messages cannot change the endpoint,
model, limits, credentials or configuration file. Configuration status exposes
only enabled/configured state, model, provider and a safe explanation. It does
not probe the model server or establish that the model is installed.

For same-server Ollama deployment, set **`OLLAMA_NO_CLOUD=1` in the Ollama
service's environment** and restart that service when applying the setting.
Ollama documents this local-only setting in its [cloud configuration FAQ](https://docs.ollama.com/faq#how-do-i-disable-ollama-cloud-features).
The client rejects obvious `:cloud` and `-cloud` identifiers, but model naming
alone cannot prove that a daemon or proxy performs local inference. The service
setting and deployment controls establish that boundary. No Ollama installation,
model download or service configuration change is performed by this client.

An existing internal OpenAI-compatible service can use
`"provider": "openai-compatible"`. Non-loopback endpoints require an explicit
`"allow_private_endpoint": true`. DNS must resolve exclusively to loopback,
RFC1918 IPv4 or IPv6 ULA addresses; the transport pins a validated address for
each request. Public, link-local, IPv4-mapped IPv6 and metadata-service addresses
are rejected. HTTPS is required outside loopback unless an administrator
explicitly sets `"allow_insecure_private": true`. TLS certificate and hostname
verification remain enabled. Proxy environment variables and HTTP redirects are
not used.

If an internal service requires a bearer credential, set `api_key_env` to its
secret-manager-provided environment-variable name. Do not place a secret value
in the JSON file. Its value is read only when making an inference request and
is omitted from status and error messages. Ollama's local endpoint normally
does not require this field.

Requests use `/v1/chat/completions` with text messages, function tools,
`stream: false`, `temperature: 0`, and `max_tokens`, as supported by
[Ollama's OpenAI compatibility API](https://docs.ollama.com/api/openai-compatibility).
The client permits 5–120 second timeouts, 64–8192 output tokens, 64 messages,
16 tool definitions and 8 returned tool calls. Requests are limited to 512 KiB,
responses to 1 MiB and each tool's arguments to 16 KiB. Truncated responses,
duplicate IDs/JSON keys, invalid arguments, unavailable tool names and malformed
output are rejected. Provider response bodies and transport exception details
are never copied into errors. The client returns proposed calls; the application
must independently validate and authorize any operation.

Run the isolated checks with:

```sh
.venv/bin/python -B tests/local_llm.py
```

These tests cover temporary configuration, actual loopback HTTP requests,
endpoint resolution/pinning, timeouts, error redaction and malformed outputs.
They do not establish inference quality, model tool-calling reliability or
availability on a deployment server.
