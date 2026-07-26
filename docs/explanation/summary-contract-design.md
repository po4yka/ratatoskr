# Summary Contract Design

Ratatoskr treats model output as untrusted structured input. A summary becomes a
durable record only after it has been shaped into the canonical Pydantic model.
The exact fields and normalization behavior are documented in
[Summary JSON Contract](../reference/summary-contract.md).

## Why a contract exists

The same summary feeds Telegram formatting, REST and sync payloads, full-text and
vector search, exports, quality diagnostics, and later enrichment. Free-form text
would make each consumer guess field names, types, and failure behavior. The
contract gives those consumers one persisted representation and keeps model
variability at the boundary.

The design optimizes for:

- stable core text and evidence fields;
- conservative uncertainty metadata;
- provider-compatible structured output;
- explicit compatibility normalization for older stored/model payloads;
- observable, bounded repair rather than silent acceptance.

## One descriptor binds the moving parts

`SummaryContractDescriptor` binds the schema name, schema loader, EN/RU prompt
loader, provider response formats, and compatibility mapper under contract ID
`default`.

This prevents a workflow from accidentally using the current prompt with an old
schema, or a provider schema without the matching normalizer. Adapters request
the descriptor; they do not assemble these pieces themselves.

## Strict generation, tolerant ingestion

The contract has two deliberately different boundaries:

1. The provider JSON Schema marks all canonical properties required, rejects
   additional object properties, and is suitable for strict structured output.
2. The compatibility mapper accepts known legacy/camelCase names and missing
   optional material, derives safe defaults, and finally validates the canonical
   Pydantic model.

Strict generation reduces new drift. Tolerant ingestion keeps existing records
and imperfect model responses usable. Tolerance is bounded: the input must be a
non-empty dictionary, the core summaries must be derivable, and the final result
must validate.

## Conservative quality semantics

Unknown evidence must remain unknown. Missing or malformed confidence becomes
`0.0`; invalid hallucination risk or classification becomes `unknown`.
Normalization warnings, repair state, model/mode provenance, extraction quality,
and source coverage are persisted under `summary_quality`.

`quality.prompt_injection_suspected` is also synchronized into
`summary_quality.prompt_injection_suspected`, so API-safe provenance does not
lose the content safety signal.

These fields describe available evidence; they are not proof that a summary is
factually correct.

## Search fields are derived at the boundary

The normalizer makes search-facing data predictable:

- topics become canonical hash tags;
- missing keywords can be extracted from summary text;
- query-expansion terms combine supplied keywords, tags, ideas, and TF-IDF;
- semantic boosters are capped standalone sentences;
- semantic chunks inherit article/topic/language context.

Qdrant remains a derived index. The persisted canonical summary and its embedding
metadata in PostgreSQL are the recovery source.

## Validation and repair live in the graph

The summarize graph has one validation/repair loop:

```text
summarize → validate ──success──> enrich → persist
                └──failure──> repair ──> validate
```

The validate node applies the compatibility mapper. Repair receives the original
messages, previous candidate, and exact validation errors, then makes another
structured LLM call. `MAX_REPAIR_ATTEMPTS` bounds this loop at three; the graph's
call budget and recursion limit add independent bounds. Every attempt is stored
in `llm_calls`.

This separates deterministic shaping from probabilistic correction and gives an
operator an attempt trail instead of a fabricated success-rate claim.

## Evolution rules

A contract change is complete only when all affected surfaces agree:

1. update `SummaryModel` and nested models/enums;
2. update normalization/shaping when compatibility behavior changes;
3. update both EN and RU prompt field declarations;
4. verify the descriptor-generated strict schema;
5. update persistence, API/sync serializers, formatters, and search consumers;
6. add contract, graph, and parity regression tests;
7. update the reference documentation.

Changing `SUMMARY_PROMPT_VERSION` invalidates prompt-scoped cache entries; it does
not create a new registered summary contract. Add a new descriptor ID only when
two independently supported wire shapes genuinely need to coexist.

## Trade-offs

The schema adds coordination cost and large structured prompts can challenge
smaller models. In return, downstream code does not need defensive field-name
guessing, repair is observable, and persisted summaries have stable types.

The compatibility mapper can conceal recurring provider defects if warnings and
attempts are ignored. Monitor repair counts and `summary_quality.validation_warnings`;
promote frequent normalization into prompt/provider fixes instead of indefinitely
expanding aliases.

See [Graph and Agent Architecture](multi-agent-architecture.md) for the surrounding
workflow and [Validating Summaries](../reference/summary-contract.md#using-the-contract)
for the focused test command.
