# Segmented Strategy Draft Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Add a structured `draft.segments + compiled_dsl` generation flow so NL2DSL can represent multi-stage chart patterns before saving executable DSL.

**Architecture:** Add a non-executable draft schema and a small compiler/coverage layer beside the existing DSL. Keep existing `StrategyDSL` and engine compatible; LLM parsing accepts both the new result envelope and legacy direct DSL output. Streamlit displays draft segment understanding before saving the compiled DSL.

**Tech Stack:** Python 3.12, Pydantic v2, Streamlit, pytest.

---

## Chunk 1: Draft Schema And Coverage

### Task 1: Strategy Draft Schema

**Files:**
- Create: `core/strategy_draft.py`
- Test: `tests/test_strategy_draft.py`

- [x] **Step 1: Write failing schema tests**

Add tests for a valid three-segment trigger draft, invalid window order, lookback shorter than segment span, and trigger mode without a trigger segment.

- [x] **Step 2: Run red test**

Run: `.venv/bin/python -m pytest tests/test_strategy_draft.py -v`
Expected: FAIL because `core.strategy_draft` does not exist.

- [x] **Step 3: Implement minimal schema**

Create Pydantic models:
- `SegmentWindow(start:int, end:int)`
- `StrategySegment(name, role, window, intent, technical_description, rules)`
- `StrategyDraft(name, description, timeframe, lookback, mode, segments)`
- `StrategyGenerationResult(draft, compiled_dsl)`

- [x] **Step 4: Run green test**

Run: `.venv/bin/python -m pytest tests/test_strategy_draft.py -v`
Expected: PASS.

### Task 2: Coverage And Default Compiler

**Files:**
- Create: `core/strategy_compiler.py`
- Modify: `tests/test_strategy_draft.py`

- [x] **Step 1: Write failing coverage/compiler tests**

Add tests that:
- draft with setup/pullback/trigger returns coverage warnings when DSL lacks window, offset, and trigger semantics.
- three-segment draft compiles to a valid `StrategyDSL` containing `HHV/LLV/HHVBARS`, `offset`, and `cross_up`.

- [x] **Step 2: Run red test**

Run: `.venv/bin/python -m pytest tests/test_strategy_draft.py -v`
Expected: FAIL because compiler functions do not exist.

- [x] **Step 3: Implement minimal compiler**

Add:
- `validate_draft_coverage(draft, dsl) -> list[str]`
- `compile_draft_defaults(draft) -> StrategyDSL`
- small helper functions to inspect rules for indicator/op usage.

- [x] **Step 4: Run green test**

Run: `.venv/bin/python -m pytest tests/test_strategy_draft.py -v`
Expected: PASS.

---

## Chunk 2: LLM Parsing And Prompt

### Task 3: Parse New Envelope While Preserving Legacy DSL

**Files:**
- Modify: `core/llm_client.py`
- Test: `tests/test_llm_client.py`

- [x] **Step 1: Write failing parser tests**

Add tests for:
- direct legacy DSL JSON still parses.
- `{draft, compiled_dsl}` parses and returns both compiled DSL and generation result.
- invalid coverage returns an error.

- [x] **Step 2: Run red test**

Run: `.venv/bin/python -m pytest tests/test_llm_client.py -v`
Expected: FAIL because parser/result support does not exist.

- [x] **Step 3: Implement parsing result**

Add an internal parser helper returning `(dsl, generation_result, warnings_or_errors)` and update `generate_strategy` return tuple to include draft result while keeping old callers safe.

- [x] **Step 4: Run green test**

Run: `.venv/bin/python -m pytest tests/test_llm_client.py -v`
Expected: PASS.

### Task 4: Prompt Output Shape

**Files:**
- Modify: `core/strategy_prompt.py`

- [x] **Step 1: Update system prompt**

Require a JSON object with `draft` and `compiled_dsl` for non-trivial staged chart descriptions. Keep legacy-compatible `compiled_dsl` schema.

- [x] **Step 2: Update few-shot**

Change the staged example to show three segments and a compiled DSL. Keep simple examples understandable.

- [x] **Step 3: Sanity check prompt syntax**

Run: `.venv/bin/python -m py_compile core/strategy_prompt.py`
Expected: PASS.

---

## Chunk 3: UI Preview

### Task 5: Display Segment Understanding

**Files:**
- Modify: `pages/2_🎯_策略生成.py`

- [x] **Step 1: Add session state**

Store `pending_generation` beside `pending_dsl`.

- [x] **Step 2: Render draft preview**

When present, show segment name, window, role, intent, and technical description above the executable DSL.

- [x] **Step 3: Surface coverage warnings**

Show warnings from parsing/coverage if present; do not silently save when DSL failed coverage validation.

- [x] **Step 4: Syntax check page**

Run: `.venv/bin/python -m py_compile pages/2_🎯_策略生成.py`
Expected: PASS.

---

## Chunk 4: Verification

### Task 6: Full Test Sweep

**Files:**
- No new files.

- [x] **Step 1: Run focused tests**

Run: `.venv/bin/python -m pytest tests/test_strategy_draft.py tests/test_llm_client.py -v`
Expected: PASS.

- [x] **Step 2: Run existing relevant tests**

Run: `.venv/bin/python -m pytest tests/test_engine.py tests/test_repo.py tests/test_tushare_api.py -v`
Expected: PASS.

- [x] **Step 3: Run diff check**

Run: `git diff --check`
Expected: no output.

- [x] **Step 4: Report changed files and behavior**

Summarize new draft flow, compatibility path, and any remaining limitations.
