# Claude API responses for the AI tests

Response bodies in the shape the Messages, Files and Message Batches APIs return them
(platform.claude.com API reference, October 2026), with made-up ids. The tests serve them
through `httpx2.MockTransport`; nothing here calls the API.

- `message_verdict.json`: a triage answer (adaptive thinking block, then the JSON text) with
  a 1-hour cache write and a cache read.
- `message_verdict_mixed_case.json`: enum values in other letter cases.
- `message_invalid.json`: an answer that does not match the schema.
- `message_refusal.json`: `stop_reason: refusal`.
- `error_overloaded.json`, `error_bad_request.json`: error bodies (529, 400).
- `file_upload.json`: a Files API upload.
- `batch_in_progress.json`, `batch_ended.json`: a batch; the tests write its results (JSON lines) from `message_verdict.json`.
