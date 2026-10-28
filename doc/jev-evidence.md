# Optional Jev evidence assessment

Jev is an optional shared **tool**, not another workflow Agent. The existing
PatientState → Review → Target → Review → Drug → Review sequence is unchanged.
It supplies narrow semantic judgments; code still owns missing-field checks,
unit arithmetic, cutoff boundaries, execution budgets and safety constraints.

## Enable or disable

The switch lives in [`config/jev.jsonc`](../config/jev.jsonc), referenced by
`jev_file` in [`config/tools.jsonc`](../config/tools.jsonc):

```jsonc
{
  "enabled": false, // true exposes assess_evidence; false disables it completely
  "allow_patient_data": false // true permits selected cutoff-safe patient evidence to be sent to TypeSafe
}
```

These are excerpts, not a replacement for the complete configuration. With Jev
disabled, the workflow does not register its tool, load its rubrics, require its
API key or construct its HTTP client. Other Agents' existing providers still load
their own credentials normally.

To enable real calls:

1. Confirm the applicable patient-data and source-content permissions cover
   processing by TypeSafe. A public/de-identified dataset is not by itself a
   universal permission to send its contents to any external provider.
2. Set `TYPESAFE_API_KEY` in the environment, or add that key to the existing
   `config/secrets.local.json`. Preserve other keys. Environment values take
   precedence, including an explicit empty value, which causes a configuration
   error rather than falling back to a different credential. Never put keys in
   tracked model/tool files. `secrets.example.json` contains an empty placeholder.
3. Set `enabled: true`. Set `allow_patient_data: true` if patient records are to be
   assessed; it can remain false for document-only assessment.
4. Use workflow `live` mode and re-create `WorkflowConfiguration` and
   `AgentWorkflow`. In a notebook, rerun the configuration/construction cell.

Set `enabled: false` and re-create the workflow to turn it off. Settings are
snapshotted; editing configuration does not hot-reload or interrupt an active
request. Rubrics are fixed for one run. Demo mode never constructs a live Jev
client; explicit fake-client injection is available only for offline tests/demo.

The native endpoint is `https://api.typesafe.ai/v1/systemone`, not the existing
OpenAI-compatible provider adapter. The integration uses the documented HTTP
contract and `httpx` (`pip install -e '.[jev]'`). It does not require adding a fifth
chat provider to `providers.jsonc`. The pinned default model is `jev-1.14.0`;
confirm account availability before live use. No paid API call or medical-domain
calibration was performed as part of this implementation.

## Configuration ownership

| File | Purpose |
| --- | --- |
| `config/jev.jsonc` | Switch, endpoint, model, key environment name, patient-data opt-in, timeout, budgets, review flag threshold. |
| `config/jev-rubrics.jsonc` | Versioned question instructions, Choice options, ordered relevance Score levels. |
| `config/secrets.local.json` | Local key value; ignored by Git. |
| `src/tools/jev.py` | Native transport, answer validation, shared budget and audited tool. |
| `src/workflow/agents.py` | Register tool, resolve cutoff-safe evidence and validate assessment references. |
| `src/workflow/runner.py` | One evaluator per run; give all task assessments to Review. |

Rubric text can be edited in configuration. Preserve the fixed question/option
contract, including explicit insufficient-evidence outcomes. Change the rubric
version when its meaning changes. Content hashes record actual changes even if
someone forgets to increment the version. Jev's default question language is
English; evaluate non-English claims separately before relying on them.

## Tool input

`assess_evidence` supports:

- `support`: does the evidence support or contradict one claim, or is it
  insufficient/not applicable?
- `consistency`: do comparable records agree, conflict, or lack enough context?
  Changes across time are not automatically contradictions.
- `relevance`: how directly does evidence address the stated question? A separate
  assessability Choice runs in the same request. Unassessable relevance is not zero.

Example tool arguments (replace paths/references with actual snapshot values):

```json
{
  "assessment_type": "support",
  "statement": "The treatment is documented as ongoing at the cutoff.",
  "patient_fields": ["/treatments/0"],
  "patient_evidence_refs": [],
  "document_refs": []
}
```

The example pointer is illustrative; use the actual canonical `patient_state`
layout, not compact-table row coordinates. Select enough record context to retain
units, dates and missingness. Patient fields are resolved inside the frozen
cutoff-safe snapshot; arbitrary filesystem paths, other patients and evaluation
labels cannot be requested. `null`, `false`, zero and explicit field-status
annotations remain unchanged. Raw patient evidence references must belong to
the same snapshot. The Agent must first read the extracted shared state.

Document inputs use exact `document_id:sha256:section_id` citations returned by
guideline tools. The host re-fetches the complete section, checks its hash and
current processing/external-model permissions, and preserves source qualifiers.
NCCN's existing permission block remains in force. Long sections/requests fail
with no model call rather than silently dropping text. The statement is a claim
or query, **never an authenticated source fact**. No arbitrary source text is
accepted as a tool argument. Only selected evidence is sent, not the whole patient
or the complete conversation.

## Result semantics

Successful outputs contain:

- `assessment_id`, assessment type and the selected label or relevance score;
- `judge_confidence`, full `probabilities` and typed `answers`;
- requested rubric version and source references; resolved model and token usage;
- `review_flags`, `review_required: true`, `clinical_validation: not_performed`;
- `automatic_weight_change: false`.

Confidence describes concentration of Jev's judgment distribution, **not patient
data reliability, clinical correctness or drug efficacy**. High confidence in
`insufficient_evidence` means the model confidently found the evidence insufficient.
Low confidence is not proof of a bad record. Relevance is a probability-weighted
position on the configured levels; it is not the probability a drug is suitable.

`normalized_relevance` is returned only when both assessability and relevance
meet the configured confidence flag threshold. Otherwise it remains null. Raw
answers remain available for inspection. The default `0.8` is a provisional
review flag, not a validated medical threshold, and even a high-confidence result
still requires review. No score is multiplied by confidence or used to override
necessary conditions, patient exclusions, or existing empty-candidate guards.

Service errors, missing credentials, unknown/forbidden references, insufficient
budgets, invalid responses and offline mode produce explicit non-assessment
statuses. `assessment` and `judge_confidence` stay null, not invented zeros.
An API error is distinct from Jev selecting `insufficient_evidence` successfully.
Jev does not return a reasoning explanation; any human/Review explanation must
be identified as a separate interpretation grounded in the cited evidence.

## Review, output and audit

Every call, including failed attempts, is written to:

```text
workspace/<date>/<run>/<turn>_<Agent>/jev/<sequence>_jev_<id>.json
```

For attempted requests, this records selected inputs, rubric hash, request hash,
validated response, model and usage. Invalid provider bodies and raw provider
exceptions are not persisted. API keys are excluded. The input audit can contain
patient data: it is not an anonymization mechanism, so protect the workspace and
do not publish it.

Each turn's `evidence_assessments` is passed to Review inside `stage_result`, even
if the task Agent omits an inconvenient result from its final answer. Structured
outputs may use `assessment_refs`; the host accepts only successful assessment IDs
from that same turn. Assessment IDs are not patient or document citations. Jev
reading a document does not falsely mark it as fully read by the task model.

`workflow_result.json` records enabled settings, rubric version/hash and total
requests attempted. No patient state, original evidence or source weights are
modified by this tool. Review remains responsible for independently checking
important claims; Jev's output does not automatically approve or reject a stage.

## Budgets and failure behavior

Defaults are four provider attempts per Agent turn and 24 per workflow run,
including failed attempts. Reviews and retries share the run budget. There is no
cross-Agent cache, so a Review assessment is a new judgment, not replayed task
output. The request cap is 24,000 UTF-8 bytes including questions; it is not an
exact token limit. The response cap is 64,000 bytes and the HTTP timeout is 30
seconds. Redirects, automatic retries and environment proxy inheritance are off.
Workflow API routing uses `workflow.jsonc.proxy_port` (currently `28374`); `null`
selects direct access. This explicit route also applies to the notebook smoke test.
Rate limiting/overload is reported as `http_429`/`http_529`; another explicit tool
call consumes another attempt. The integration does not silently enable long
context or images.

The runtime validates question IDs, answer types, complete option distributions,
finite numbers, probability sums, selected options, Score expectations/legends
and token usage. These are interface checks, not proof of truth or calibration.

## Verification and next evaluation step

```bash
.venv/bin/python -m pytest -q tests/test_jev.py tests/test_workflow_config.py
```

Offline tests use synthetic records, a fake Jev evaluator and an HTTP mock. They
cover enable/disable behavior, credential precedence, privacy/permission gates,
time boundaries, unchanged patient state, budgets, failure semantics, actual
native API payloads, and propagation to Review. Before using scores for ranking,
evaluate on independently labeled BPC cases, including missing, contradictory and
corrected records. Pin model and rubric versions and examine high-confidence
errors; do not optimize prompts solely to increase confidence.

Design references: [native API](https://docs.typesafe.ai/api/system-one),
[confidence](https://docs.typesafe.ai/confidence),
[Choice](https://docs.typesafe.ai/primitives/choice),
[Score](https://docs.typesafe.ai/primitives/score),
[citation checking](https://docs.typesafe.ai/cookbooks/citation_check).
