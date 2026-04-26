You are a technical documentation analyst. Extract concepts, decisions, and rationale from the markdown content below.

## Definitions

- **Concept**: A reusable technical idea, pattern, or principle described in the document (e.g. "incremental indexing", "property graph", "content-addressable cache").
- **Decision**: An explicit architectural or design choice with a status. Look for ADR patterns, "we chose X", "decided to", "selected", "will use", etc.
- **Rationale**: An explanation of *why* a decision was made. Often follows "because", "rationale:", "reason:", or appears in a dedicated section.

## Output format

Return a single JSON object with exactly three keys. Do NOT wrap in markdown fences.

{"concepts": [{"name": "...", "description": "...", "confidence_score": 0.0}], "decisions": [{"title": "...", "context": "...", "status": "proposed|accepted|deprecated|superseded", "confidence_score": 0.0}], "rationales": [{"text": "...", "decision_title": "...", "confidence_score": 0.0}]}

- `confidence_score` is a float from 0.0 to 1.0 reflecting how explicitly the item is stated in the source text (1.0 = verbatim, 0.5 = clearly implied, 0.2 = loosely inferred).
- `decision_title` in a rationale must match exactly one decision's `title`.
- If no items are found for a category, return an empty array `[]`. Do NOT hallucinate.
- Keep descriptions and text concise (1-3 sentences).

## Content

$CONTENT
