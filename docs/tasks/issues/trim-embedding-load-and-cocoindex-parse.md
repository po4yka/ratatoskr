---
title: Trim embedding load and CocoIndex parse
status: backlog
area: observability
priority: low
owner: unassigned
epic: epic-optimize-vector-and-embedding-pipeline
blocks: []
blocked_by: []
created: 2026-05-28
updated: 2026-05-28
---

- [ ] #task Trim embedding load and CocoIndex parse #repo/ratatoskr #area/observability #status/backlog 🔽

## Objective

Three small inefficiencies/risks: a probe `model.encode("test")` forward pass runs on every model load just to read the (constant) vector dimension; CocoIndex parses each row's `json_payload` twice; and the summary scroll lacks an `entity_type` filter on a collection shared by two flows (latent cross-entity bug).

## Context (evidence)

`app/infrastructure/embedding/embedding_service.py:84-92` (probe `encode("test")` for dimensions); `app/infrastructure/cocoindex/flow.py:31-64` and `:67-102` (`_extract_indexable_text` and `_build_qdrant_payload` each `json.loads` the same payload); `app/infrastructure/cocoindex/runtime.py:60-77` + `qdrant_store.py:476` (two flows share one collection; summary scroll has no entity_type filter).

## Scope

Hardcode/derive the dimension from the model registry instead of a probe pass; parse `json_payload` once per row and pass the parsed dict to both helpers; add an `entity_type` filter to the summary scroll (and ensure payloads carry an entity_type).

## Acceptance criteria

- No probe forward pass on model load.
- Payload parsed once per row.
- Summary scroll cannot return repository points.
- Tests cover the entity_type filter.

## Epic

Part of [[epic-optimize-vector-and-embedding-pipeline]].

## References

- Performance audit findings E-3, S-2, CO-2 (2026-05-28).
