# Knižky od Simona – AI Import: API for the external processing service

The service downloads uploaded PDF/ZIP batches, analyses, translates to Slovak and splits pages into themed collections, uploads the processed files and sends collection proposals back. Proposals stay **drafts** until an administrator approves them in `/admin/import`. The service can never publish anything or write to the customer library.

## 1. Basics

- **Base URL (production):** `https://knizkyodsimona.sk/api/public/import/<action>`
- **Base URL (preview/testing):** `https://project--8b8ebdc5-9d88-4cee-bcce-aa4d1eb397a8-dev.lovable.app/api/public/import/<action>`
- **Method:** every action is `POST` with a JSON body (`Content-Type: application/json`). Send `{}` when an action has no fields.
- **Authentication:** header `Authorization: Bearer <IMPORT_SERVICE_TOKEN>`. The token is stored as a secret in the website and given to the service operator; never commit it.
- Every authenticated call (any action) also counts as a heartbeat.

### Common error responses

| HTTP | body | meaning |
|---|---|---|
| 503 | `{"error":"integration_not_configured"}` | the website has no token configured yet |
| 401 | `{"error":"unauthorized"}` | missing/wrong token |
| 404 | `{"error":"unknown_action"}` / `{"error":"batch_not_found"}` | |
| 400 | `{"error":"invalid_body","details":[...]}` | body failed validation (first 5 issues) |
| 400 | `{"error":"path_outside_batch"}` / `{"error":"invalid_name"}` | a storage path is not inside `batches/<batch_id>/` or contains `..` |
| 500 | `{"error":"upload_url_failed"}` | retry later with backoff |

Retry only on 5xx / network errors (exponential backoff). 4xx means the request must be fixed.

## 2. Triggering: polling, no webhook

There is **no webhook**. The service must poll:

1. Call `heartbeat` at least every 10 minutes (admin UI shows "connected" only if contact was within the last 15 minutes).
2. Call `claim` every 30–60 seconds while idle. It returns `{"batch": null}` when nothing is waiting.
3. Process one claimed batch at a time (or a few in parallel – `claim` is atomic, a batch is handed out only once).

A batch is claimable in status `queued` (new upload or admin pressed "Opakovať") or `rebuild` (admin changed collection boundaries). On claim it switches to `processing`. If the service crashes, it must report `fail` – otherwise the batch stays in `processing`.

## 3. Endpoints

### `heartbeat`
Request: `{ "version": "1.0.3" }` (optional, max 100 chars, shown in admin)
Response: `{ "ok": true }`

### `meta`
Request: `{}`
Response – allowed values for proposals:
```json
{
  "categories":   [{ "id": "omalovanky", "label": "Omaľovánky" }, ...],
  "languages":    [{ "id": "bez-textu", "label": "Bez textu" }, { "id": "sk", ... }, { "id": "en", ... }, { "id": "mix", ... }],
  "difficulties": [{ "id": "jednoduche", ... }, { "id": "stredne", ... }, { "id": "narocnejsie", ... }],
  "themes":       [{ "name": "Zima", "english": false }, ...]
}
```
Category ids: `omalovanky, bludiska, grafomotorika, cisla, pismena, logicke-ulohy, vystrihovacky, hry, anglictina, citanie`. Use theme **names** exactly as returned (case-insensitive match); unknown themes are ignored.

### `claim`
Request: `{}`
Response when idle: `{ "batch": null }`
Response:
```json
{
  "batch": {
    "id": "uuid",
    "name": "Zimné pracovné listy",
    "mode": "full",
    "files": [{ "id": "uuid", "original_name": "zima.zip", "kind": "zip", "size_bytes": 1234567, "status": "queued" }],
    "rebuild": []
  }
}
```
- `mode: "full"` – process all `files` from scratch.
- `mode: "rebuild"` – `files` is empty; `rebuild` lists collections whose pages the admin moved/split/merged and whose PDF must be regenerated:
  `[{ "id": "collection-uuid", "title": "…", "pages": ["batches/<id>/processed/0001.png", ...] }]` (pages in final order).

### `files`
Request: `{ "batch_id": "uuid" }`
Response: `{ "files": [{ "id": "uuid", "name": "zima.zip", "kind": "pdf|zip", "url": "https://…signed…" }] }`
Download with plain `GET` on `url` (valid 1 hour). ZIP files must be extracted by the service (they may contain PDFs and/or images).

### `download-urls`
Request: `{ "batch_id": "uuid", "paths": ["batches/<id>/processed/0001.png", ...] }` (1–500 paths, all inside the batch)
Response: `{ "files": [{ "path": "…", "url": "https://…signed…" }] }` – used mainly in `rebuild` mode to fetch processed pages. Valid 1 hour.

### `upload-urls`
Request: `{ "batch_id": "uuid", "names": ["orig/0001.png", "0001.png", "c1.pdf", "c1.jpg"] }`
- 1–500 names per call; each name 1–200 chars, only `A-Z a-z 0-9 _ . - /`, no `..`.
Response:
```json
{ "uploads": [{ "name": "c1.pdf", "path": "batches/<id>/processed/c1.pdf", "url": "https://…signed upload…" }] }
```
Upload each file with `PUT <url>`, raw file bytes as body and the correct `Content-Type` (`application/pdf`, `image/png`, `image/jpeg`, `image/webp`). Existing files at the same path are overwritten. Max file size 5 GB. Afterwards reference the returned `path` (not the URL) in `results`.

### `progress`
Request:
```json
{ "batch_id": "uuid", "progress": 40, "file_id": "uuid", "file_status": "processing", "error": "optional text" }
```
`progress` integer 0–100 (whole batch). `file_id`/`file_status` (`processing|done|failed`)/`error` optional. Response `{ "ok": true }`. Send it every few pages so the admin sees movement.

### `fail`
Request: `{ "batch_id": "uuid", "error": "Slovak or English description (max 2000)", "file_id": "uuid (optional)" }`
Marks the batch (and the file, if given) as failed; the admin sees the message and can press "Opakovať", which re-queues it. Response `{ "ok": true }`.

### `results`
Final submission. Batch status becomes `review` ("Na kontrolu"), progress 100.

**Full mode** – replaces all previous draft proposals and all pages of the batch:
```json
{
  "batch_id": "uuid",
  "mode": "full",
  "collections": [
    {
      "key": "c1",
      "title": "Zimné omaľovánky – snehuliaci",
      "description": "12 omaľovánok so zimnou tematikou.",
      "category": "omalovanky",
      "theme_names": ["Zima"],
      "language": "sk",
      "difficulty": "jednoduche",
      "page_count": 12,
      "result_pdf_path": "batches/<id>/processed/c1.pdf",
      "preview_path": "batches/<id>/processed/c1.jpg",
      "file_hash": "sha256 hex of the result PDF"
    }
  ],
  "pages": [
    {
      "collection_key": "c1",
      "file_id": "uuid of source file from claim (or null)",
      "source_name": "zima.zip/strana-01.pdf",
      "page_no": 1,
      "original_path": "batches/<id>/processed/orig/0001.png",
      "processed_path": "batches/<id>/processed/0001.png",
      "detected_language": "en",
      "needs_review": true,
      "flag_note": "Slovná hra 'snowman' sa nedá presne preložiť."
    }
  ]
}
```
Field rules:
- `collections` max 500. `title` 1–200 chars (required), `description` max 2000 (default ""), `category` one of the ids above or `null` (admin then must choose), `theme_names` max 30, `language` `bez-textu|sk|en|mix|null`, `difficulty` `jednoduche|stredne|narocnejsie|null`, `page_count` 1–5000 or null, paths max 500 chars, `file_hash` max 128.
- `pages` max 20 000, in the final reading order (the order defines page positions). `collection_key` must equal a `key` in `collections` (or null = unassigned page). `page_no` ≥ 1 (page number in the source file).
- `original_path` / `processed_path`: page images (PNG/JPG/WEBP recommended – the admin compares them side by side). Other formats are shown only as a link.
- `needs_review: true` highlights the page as "Preklad na kontrolu" with `flag_note` (max 1000).
- All paths must start with `batches/<batch_id>/`.
- The website checks duplicates against the library itself (same `file_hash`, or same title in the same category) and warns the admin.

**Rebuild mode** – only regenerated PDFs for the collection ids received in `claim.rebuild`:
```json
{
  "batch_id": "uuid",
  "mode": "rebuild",
  "collections": [
    { "id": "collection-uuid", "title": "same title as received", "result_pdf_path": "batches/<id>/processed/c1-v2.pdf",
      "preview_path": "batches/<id>/processed/c1-v2.jpg", "page_count": 9, "file_hash": "sha256 hex" }
  ],
  "pages": []
}
```
Only `result_pdf_path`, `preview_path`, `page_count`, `file_hash` are updated; titles/categories edited by the admin are kept.

## 4. Recommended processing flow

```text
loop:
  heartbeat (every ≤10 min)
  b = claim()
  if b is null: sleep 30–60 s; continue
  try:
    if b.mode == "full":
      files = files(b.id)                    # signed GET URLs
      download, unzip ZIPs, render every page to an image (orig/NNNN.png)
      detect language, translate to Slovak, re-typeset the page (NNNN.png)
      flag uncertain translations (needs_review)
      group pages into themed collections, pick category/themes/language/difficulty
      build one A4 PDF per collection + cover image (first page JPG)
      progress(...) regularly
    else:  # rebuild
      for each c in b.rebuild: download-urls(c.pages) -> rebuild PDF + cover
    upload-urls(...) -> PUT every file
    results(...)
  except error:
    fail(b.id, message)
```

## 5. Rules the service must follow

- Never invent results: if analysis or translation fails, call `fail` with the reason.
- Store nothing outside `batches/<batch_id>/`; do not keep customer data (there is none in this API).
- Respect the licence recorded with the batch (shown to the admin); the service does not decide about publishing.
- Keep the token secret; rotate it by asking the website administrator.
