# Knižky od Simona — AI Import worker

External polling worker implementing `import-service-api.md`. It never writes to the customer library or publishes collections. Requires Lovable import API token and an OpenAI API key.

## Deployment (Railway / Render background worker / any Docker VPS)

1. Publish this folder to a **private Git repository** (do not commit secrets).
2. Create a Docker-based **background worker** service; Dockerfile is included. **One replica only** for the first deployment.
3. Configure secrets from `.env.example` in the hosting provider dashboard. Use the production base URL after publishing the Lovable site; preview URL is only for tests.
4. Ask Lovable to create `IMPORT_SERVICE_TOKEN` via its secure form, then configure the SAME token as a secret on the worker. Never paste it into chat or Git.
5. Create an OpenAI API key, set `OPENAI_API_KEY`. Use a model supporting vision + JSON object output (default `gpt-4.1-mini`; verify access on your account).
6. Deploy. Logs should show heartbeat and polling; the admin `/admin/import` should show a connected service within ~1 minute.
7. Upload a **small test PDF** (2–5 pages) and verify original/processed previews, translated instructions, grouping, category, review flags and PDF download. Only then scale up.

Local run: `python -m pip install -r requirements.txt` and set environment variables, then `python -m app.main`. OCR requires `tesseract` installed (Docker includes it). Tests: `pytest -q`.

## How it works

- Polls `claim` every 45 seconds; all API requests also heartbeat.
- Downloads signed files; extracts PDFs/images from ZIPs with file-count and decompressed-size limits.
- Renders pages to A4 images; OCRs English text with Tesseract.
- Sends each page image and OCR lines to an OpenAI vision model. AI returns classification, collection label, translations and review flags.
- **Conservatively overlays translated text on the page image. Every page with an overlay is flagged for review.** This is not layout-perfect, and original illustrations beneath the OCR bounding box may be obscured. Check these pages before approval.
- Groups consecutive pages from the same source with the same AI collection label and category; generates A4 PDFs, previews, page comparisons, and upload metadata.
- Rebuilds PDFs from processed page images when the admin edits boundaries.
- Uses API `results` to submit **draft proposals**; does not publish.

## Important limitations and operational risks

- This is a **functional first version, not a validated production-quality translation engine**. Scanned/image text is detected by English OCR, which can miss decorative text. The model is instructed not to translate English learning exercises, but human review remains essential. Any page changed by overlay is flagged.
- Images are preserved except where a translated text overlay covers pixels. Do not approve without checking such pages.
- AI classification may create overly small collections if grouping names vary. Merge in admin and rebuild.
- Large batches are processed sequentially, can be slow/costly, and results are sent only after all files are processed. On worker termination mid-batch the API currently has **no lease expiry/recovery**; the admin must reset the stuck batch (Lovable needs to add stale-processing recovery).
- Uploading many large PNGs may consume substantial cloud storage. Use `MAX_TOTAL_PAGES` to cap batches and test storage pricing.
- No persistent checkpointing in v1. A batch requeued after failure is reprocessed from scratch. A durable queue/checkpoint system is recommended before importing thousands of pages.
- The worker does not receive the recorded license fields in the claim response; the admin is responsible for verifying rights before approval.
- API only supports `pdf|zip` for top-level uploads, but images inside ZIPs are supported.
- Signed upload URLs may expire on long uploads; they are requested in chunks immediately before upload.
- If `meta` returns an empty category list or the AI response is invalid, processing fails rather than fabricating a result.
- Never run with untrusted document content in a privileged environment. ZIP members are never extracted to their supplied path; unsupported types are ignored. Keep API credentials secret.

## API

See `docs/import-service-api.md` (included) for endpoint contracts. Worker uses only `heartbeat`, `claim`, `meta`, `files`, `download-urls`, `upload-urls`, `progress`, `fail`, `results`.
