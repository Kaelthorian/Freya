# Prompt debug diagnostics

## Investigation

Previously `llm_trace.debug_enabled()` queried `os.getenv` for every call and
validation event. It defaulted to false; true/1/yes/on parsing was already
correct. Startup did not load `.env`, create a configuration snapshot, or report
the effective setting. All production adapters already used the common tracer
via `transport.model_request`; neither Evaluator nor agent configuration
overrode the flag. Runtime spawned workers with the parent process environment,
without an explicit debug-settings argument. Worker credential cleanup does not
match `FREYA_DEBUG_LLM_PROMPTS`.

An assignment in a `.env` file, another shell, or a shell changed after server
startup therefore did not reach the running server. A true value actually present
in the server environment was already parsed as true. The original live process
environment was not inspected, so its exact missing-variable scenario remains
unverified. No private `.env`, runtime database or historical prompt log was read.

Independent capture gaps: tracing preceded the transport's streaming/output
adjustments, the default budget was 20000 characters (maximum 200000), and some
evidence-contract errors after JSON parsing did not reach `llm.validation`.

## Configuration

`control_center/settings.py` owns immutable `Settings`, reusable `parse_bool`,
`get_settings` and startup initialization. Only the two named debug variables
are queried. Runtime passes the same snapshot to `worker.process_main` through
spawn; children install it before task execution. Every model component,
including legacy Analyst calls, shares the common instrumentation.

Precedence: process environment then default. There is no `.env`/`.env.local`
loader or new dependency. True/1/yes/on enable debug, ignoring case and whitespace.
False/0/no/off, empty, unset and unknown strings produce false. Empty values have
source `environment`; absent values have source `default`. Default debug is false.

Set the flag in the launching terminal and restart the whole server:

```powershell
$env:FREYA_DEBUG_LLM_PROMPTS = "true"
python -m control_center --port 8765 --workers 2 --data-dir .\data
```

Before constructing Runtime, startup prints this JSON event on stdout:

```json
{"event_type":"freya.config.loaded","debug_llm_prompts":true,"source":"environment","llm_provider":"ollama","debug_prompt_max_chars":4000000}
```

It also prints `Debug LLM prompts: ENABLED`. No secrets or complete environment
dump are logged. This console event is independent of orchestration/task SQLite
logs. Both debug settings require server restart. Standalone library consumers
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

`evidence_contract` identifies post-parse evidence-claim rejection. The shared
observer only logs and re-raises the original exception. It changes no matcher,
criterion, semantic decision, recovery/retry, planning, integration or tool
execution. `request_body.format` identifies the schema without repeating it in
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

## Tests and remaining validation

`tests/test_settings.py`: unset/boolean variants/invalid values, budgets, startup
event, restart semantics, Runtime arguments and real spawned-worker propagation
with a conflicting child environment.

`tests/test_llm_trace.py`: all nine registered profiles and real adapters plus
Worker in both modes, intent matcher, the supplied saludo regression prompt with
synthetic evaluator evidence, initial/repair correlation, observational invariance,
effective request against a fake loopback streaming provider, raw/parsed/normalized
separation, output/evidence contract rejection, redaction, nested scopes and large
prompt persistence/API sanitization. Fixtures are synthetic and disposable.

Docker was unavailable and the user requested keeping the Docker requirement and
leaving Python checks pending. Tests, compilation, CLI execution, actual startup
and live Ollama/Qwen orchestration remain unverified. Static review does not
prove runtime behavior. All six frontend `node --check` commands succeeded;
they do not validate the Python changes. Focused validation is
`python -m unittest tests.test_settings tests.test_llm_trace -v`, followed by the
repository full checks, in the authorized Docker validation environment.

After restart in a development instance, submit:

```text
Crea un archivo saludo.txt con el texto Hola mundo, después agregá una segunda línea que diga Freya funciona. Finalmente verificá que el archivo contenga ambas líneas.
```

First check stdout for the true startup flag. Inspect all `llm.call` flags and
Evaluator `request_body.messages`, `request_body.format`, `structured_context`,
`raw_response` and correlated `llm.validation`. Repairs must have distinct IDs,
their own request/response, and a link to the rejected call. Deterministic paths
that make no model call emit no `llm.call`; debug never forces additional calls.
This live regression has not been run against Qwen during this change.
