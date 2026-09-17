# Local model protocol assessment

After the local model has been installed and the controller's protected
`assistant-config.json` has been enabled, run `scripts/model_acceptance.py` on
the patching server as the controller service account. It uses the production
`webapp/local_llm.py` client and the same configuration selected through
`OPU_ASSISTANT_CONFIG` or `OPU_WEBAPP_STATE_DIR`. See [Local LLM configuration](LOCAL_LLM.md).
It does not install a model, change configuration, start services or substitute
a simulator when the configured endpoint fails.

Only a loopback endpoint (`127.0.0.1`, `::1` or `localhost`) is accepted for this
same-server assessment. Private-endpoint permission overrides must be absent
or false. The client submits the configured model identifier,
authentication, output limit and timeout to `/v1/chat/completions`. The full
validated configuration is checked again before every call; changing it during
assessment stops further requests. The tool does not independently attest the
server's actual model weights or prove that an inference proxy stays local.
Keep the Ollama service's `OLLAMA_NO_CLOUD=1` deployment setting in force.

On the deployed server, using the service account and installed virtual environment:

```sh
# The receipt directory must already exist, be owned by this account or root,
# and not be writable by others. No model is installed by this command.
sudo -u opu-controller env \
  OPU_ASSISTANT_CONFIG=/etc/oracle-patching/assistant-config.json \
  /opt/oracle-patching/current/.venv/bin/python -B \
  /opt/oracle-patching/current/scripts/model_acceptance.py \
  --output /var/lib/oracle-patching/controller/model-assessment.json
```

Use a new output filename for every run. The program exclusively creates one
mode-0600 receipt and refuses to overwrite files or follow output symlinks.
It does not create conversations, saved evidence, proposals or runtime state.
Imports do not create Python bytecode caches. No shell, SSH, Oracle command,
native tool function, approval or model-selected action is executed. Only the
configured inference endpoint receives requests, containing synthetic inputs.

The six bounded, single-request cases cover a saved-host inspection call, an
exact patch-plan proposal, missing requirements, an unresolved execution,
requests outside the assistant's authority, and instructions embedded in
untrusted evidence. Proposed arguments are checked by the production typed
validator and compared with exact synthetic host, database, patch, plan and
window values. Refusal cases require a specified JSON response; a natural-language
refusal may therefore fail this protocol check despite being appropriate in chat.
The assessment sends the production system policy followed by literal examples
of the required refusal objects. A supplied host is inspected directly, and a
proposal must be a real function call: prose or JSON claiming that preparation
has happened does not pass. The production policy likewise requires a successful
controller proposal record before the assistant may claim preparation.
There are no retries. A transport, configuration or malformed-response error
stops the run; a valid but incorrect response is recorded and the next case runs.
Maximum inference time is six times the configured timeout, plus local overhead.

Exit status is `0` when all six cases pass with unchanged source/configuration,
`1` when assessment fails or is blocked, and `2` when the explicit receipt cannot
be created or saved. The receipt contains fixed outcome codes and timings,
nonsensitive effective configuration and its SHA-256, tool/schema and scenario
digests, and hashes of the assessment harness, prompt, tool contract and client.
It omits model responses, credentials, credential environment-variable names,
configuration paths and real fleet data. These hashes describe this limited
assessment scope; they are not a signed attestation of the complete release.

Every receipt is classified **model-protocol-smoke-assessment**. A pass is not
proof of reasoning safety, general tool reliability, a live patch, successful
database restore, live-lab verification or production approval. The model's
actual artifact and local inference implementation remain unverified. Review
these results alongside the application's authorization and readiness checks,
representative model evaluation and independently approved live-lab evidence.

Run the deterministic simulator tests with:

```sh
.venv/bin/python -B tests/model_acceptance.py
.venv/bin/python -B tests/local_llm.py
```

Those tests start an ephemeral loopback HTTP simulator and exercise the actual
production transport. Their receipts use `--deterministic-simulator`, which
only labels the evidence; it neither starts a simulator nor selects an endpoint.
They do not establish that a real LLM has been installed or evaluated.
