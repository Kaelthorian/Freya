# Prompt debug diagnostics

## Investigation

The normal entrypoint is `python -m control_center`. Its parent process loads
settings after CLI parsing, before Runtime and model adapters are constructed.
An already-running server retains its startup snapshot. Setting a variable in
another shell or editing source does not reconfigure that process. Historical
false flags can therefore coexist with corrected code on disk.

The inspected development process used this checkout, but its environment did
not contain `FREYA_DEBUG_LLM_PROMPTS`. There was no dotenv or database loader.
The ignored local configuration now makes the development setting durable
across normal restarts; explicit environment values still take precedence.

## Configuration

`control_center/settings.py` owns immutable `Settings`, reusable `parse_bool`,
`get_settings` and startup initialization. Only the named debug and orchestration
budget settings are queried. Runtime passes the same snapshot to `worker.process_main` through
spawn; children install it before task execution. Every model component,
including legacy Analyst calls, shares the common instrumentation.

Precedence: process environment, repository-root `.freya-local.json`, then
default. The orchestration CLI option overrides its corresponding setting.
There is no `.env`/`.env.local` loader or new dependency.
True/1/yes/on enable debug, ignoring case and whitespace.
False/0/no/off, empty and unknown strings produce false. An empty environment
value overrides a local true value. Sources record the selected environment,
local file or default; without either override, debug defaults to false.

For durable local development, create the ignored `.freya-local.json` in the
repository root (unknown setting keys fail startup):

```json
{"FREYA_DEBUG_LLM_PROMPTS":true,"FREYA_ORCHESTRATION_TIMEOUT_SECONDS":1800}
```

Alternatively set the flag in the launching terminal. Restart the whole server:

```powershell
$env:FREYA_DEBUG_LLM_PROMPTS = "true"
python -m control_center --port 8765 --workers 2 --data-dir .\data
```

After acquiring the data-directory instance lock and before constructing Runtime,
startup prints this JSON event on stdout:

```json
{"event_type":"freya.runtime.configuration","debug_llm_prompts":true,"configuration_source":"environment","repo_root":"CHECKOUT","process_working_directory":"CWD","git_commit":"HEAD","entrypoint":"python -m control_center","orchestration_timeout_seconds":1800,"orchestration_timeout_source":"default","llm_provider":"ollama","debug_prompt_max_chars":4000000}
```

It also prints `Debug LLM prompts: ENABLED`. No secrets or complete environment
dump are logged. This console event is independent of orchestration/task SQLite
logs. `GET /api/health.configuration` exposes the same non-secret snapshot.
The commit identifies HEAD; local uncommitted edits are not a new commit.
Settings changes require server restart. Standalone library consumers
initialize lazily on first access and keep the resulting snapshot.

## Event format

With debug off, `llm.call` retains call ID, component, stage, prompt name/version,
model, prompt hash/size, response hash/size, duration, status and available token
counts. `debug_prompts_enabled` is false; request, response and context texts
are absent. Provider failures retain error type/duration without an unavailable
response. Off-mode validation rejection exposes status, call ID and error type.

With debug on, existing structured field names are retained:

```json
{
  "event_type": "llm.call",
  "component": "evaluator",
  "stage": "repair",
  "llm_call_id": "REPAIR_CALL_ID",
  "repair_of_llm_call_id": "INITIAL_CALL_ID",
  "debug_prompts_enabled": true,
  "request_body": {
    "model": "fixture",
    "messages": [{"role": "user", "content": "Actual repair prompt"}],
    "tools": [],
    "format": {"type": "object"},
    "stream": true,
    "options": {"temperature": 0, "num_predict": 512}
  },
  "structured_context": {"evidence": "Actual supplied context"},
  "raw_response": "{\"criteria\": []}",
  "parsed_response": {"criteria": []},
  "parse_status": "json_parsed"
}
```

Examples omit unchanged metadata and truncation records. `request_body` is the
effective structured JSON request, including all messages, tools and schema;
there is no duplicate flattened `prompt_text`. `raw_response` is reconstructed
streamed message content before parsing, not a raw HTTP transcript or private
thinking. `parsed_response` is best-effort JSON decoding, while
`llm.validation.normalized_response` is supplied by the actual validator.

Initial and repair calls share the same path. Ordinary Worker calls retain
`worker_step`. Repairs correlate only the same component and prompt name. Nested
trace scopes restore their own call IDs and redacted response context.

Debug rejection events additionally include:

```json
{
  "event_type": "llm.validation",
  "component": "evaluator",
  "status": "rejected",
  "llm_call_id": "INITIAL_CALL_ID",
  "debug_prompts_enabled": true,
  "error_type": "EvaluationValidationError",
  "validation_message": "EvaluationValidationError: Semantic output must contain only criteria.",
  "raw_response": "{\"wrong\": true}",
  "contract_name": "evaluator",
  "contract_version": "evaluator-v1",
  "validation_stage": "output_contract",
  "call_stage": "initial"
}
```

Evaluator version 9 validates the strict semantic `output_contract`; it no
longer rejects an `unknown` rationale through diff/readback regexes. Normal
semantic input contains the prepared final snapshot and authoritative facts.
`evidence_contract` may occur in historical logs or other validators; it is not
a current Evaluator decision stage. Observers log and re-raise contract errors
without deciding criteria, changing Recovery, or executing tools. `request_body.format` identifies the schema without repeating it in
each validation event.

## Redaction and limits

All captured fields use existing `security.sanitize`: registered credentials,
credential keys/assignments, Bearer authorization, cookies, credential-bearing
URLs, recognizable key/token patterns and private keys are redacted. Existing
thinking/analysis omission remains. `Hola mundo` and `Freya funciona` remain
visible. Persistence/API sanitization also preserves the larger debug budget.
Capture never rewrites requests or parser inputs.

The retained-value character budget per field defaults to 4000000, clamped to
1000-4000000 with `FREYA_DEBUG_PROMPT_MAX_CHARS`. Oversized requests or smaller
configured budgets still report `*_truncation.truncated`, original size and
retained size. Check these markers before calling a capture complete. Redacted
captures cannot reproduce credentials or private thinking. Debug increases local
log/SQLite storage and API response sizes; keep it for development only.

## Validation

`tests/test_settings.py`: unset/boolean variants/invalid values, budgets, startup
event, restart semantics, Runtime arguments and real spawned-worker propagation
with a conflicting child environment.

`tests/test_llm_trace.py`: all nine registered profiles and real adapters plus
Worker in both modes, intent matcher, the supplied saludo regression prompt with
synthetic evaluator evidence, initial/repair correlation, observational invariance,
effective request against a fake loopback streaming provider, raw/parsed/normalized
separation, output/evidence contract rejection, redaction, nested scopes and large
prompt persistence/API sanitization. Fixtures are synthetic and disposable.

`tests/test_run_contracts.py` starts the normal entrypoint against a temporary
database and fake loopback provider, submits through the HTTP API, and checks
the actual spawned Worker's flag, messages, response and startup identity.
It also covers configuration precedence and active execution budgets.
Run `python -m unittest tests.test_run_contracts tests.test_settings tests.test_llm_trace -v`.
The case execution tests need the documented Docker image; absent Docker fails
closed. The tracing fixtures do not require a real Ollama model.

After restart in a development instance, submit:

```text
Crea un archivo saludo.txt con el texto Hola mundo, después agregá una segunda línea que diga Freya funciona. Finalmente verificá que el archivo contenga ambas líneas.
```

First check stdout for the true startup flag. Inspect all `llm.call` flags and
Evaluator `request_body.messages`, `request_body.format`, `structured_context`,
`raw_response` and correlated `llm.validation`. Repairs must have distinct IDs,
their own request/response, and a link to the rejected call. Deterministic paths
that make no model call emit no `llm.call`; debug never forces additional calls.
For live validation, use the normal entrypoint and a real installed Ollama model;
the automatic test provider proves propagation, not provider availability.
