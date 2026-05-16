# ADR 0003 — JSON Schema for SoT validation, not Pydantic

**Status:** Accepted
**Date:** 2026-05-16

## Context

Generated configs are only as trustworthy as the SoT data fed to the templates. We need to validate that data before render. Two options: Pydantic models (Python-native) or JSON Schema (language-agnostic).

## Decision

JSON Schema (`schemas/device.schema.json`, Draft 2020-12), validated via the `jsonschema` library.

## Rationale

- The same schema can be enforced by NetBox custom-validators, by a GitLab CI job written in any language, by a JSON-Schema-aware editor (VSCode, IntelliJ) at edit time, and by `generate.py` at render time. Pydantic models would be Python-locked.
- Operators reviewing PRs can read the schema directly without Python knowledge.
- JSON Schema integrates with OpenAPI (and NetBox's API spec is OpenAPI), so the validation surface aligns with the API contract.

## Trade-offs accepted

- Slightly less ergonomic than `pydantic.BaseModel` for Python-side type hints.
- Error messages from `jsonschema` are less narrative than Pydantic's.
- No automatic class generation; the `device: dict` flows through the template system as a dict rather than an attribute-access object.

## When we'd revisit

If the agent in `clab-ai-mcp` starts generating SoT input directly (LLM-proposed device additions), having Pydantic models would give the LLM a typed function-call interface. We'd add Pydantic *alongside* the schema, with the Pydantic model auto-derived from the schema (e.g., via `datamodel-code-generator`).

## See also

- [JSON Schema Draft 2020-12 spec](https://json-schema.org/draft/2020-12/json-schema-core)
- [datamodel-code-generator](https://github.com/koxudaxi/datamodel-code-generator)
