# Platform contract v1: DocFlow and the OOA Command Center

This is the agreement between DocFlow (this repository) and the OOA platform's One CommandCenter. It covers what the
Command Center calls here, and what DocFlow calls back there. `tests/test_platform_contract.py` checks it on every
push, so a change that breaks the platform fails CI here first.

The platform keeps a copy of this agreement, written from its own side, in
`docs/integration/document-automation-contract.md`.

## The rules

1. **v1 only grows.** Everything under `/api/docs/v1` that is listed below keeps its path, method, inputs and the
   response fields named here. New routes, new optional inputs and new response fields can be added at any time.
2. **Breaking changes need v2.** Removing or renaming a route or field, changing what it means, or making an input
   required starts `/api/docs/v2`. v1 keeps working for at least one platform release after that.
3. **Versions.** `VERSION` is DocFlow's version (major.minor.patch). `pyproject.toml`, `docauto.__version__` and the
   image tag in `docker-compose.yml` say the same; a test checks this. The major number changes only with a new
   contract version.
4. **The platform pins a version.** Each platform release ships one tested DocFlow version and states the range it
   works with. DocFlow's `GET /api/docs/v1/admin/version` (no token needed) gives the running version.
5. **Upgrade order.** DocFlow is upgraded first, then the Command Center. Because v1 only grows, a newer DocFlow
   always works with the Command Center already installed.

## Who calls whom

```
browser ──> Command Center /api/docflow/<route> ──(permission check)──> DocFlow /api/docs/v1/<route>
runner  ──> Command Center /api/agent/docflow/documents ─────────────> DocFlow /api/docs/v1/df/flows/{id}/documents
DocFlow ──> Command Center /api/docflow-callback/start-bot   ("Start a bot" deliveries)
DocFlow ──> Command Center /api/docflow-callback/events      (document events; see "Known gaps")
```

The browser never sees DocFlow's address or token.

## Every request from the Command Center

| Header | Value |
|---|---|
| `Authorization` | `Bearer <DOCAUTO_API_TOKEN>`. On premises the token is in the Command Center's vault (Settings > Document Automation `docflow_token_ref`). In SaaS it comes from the region's `OOA_DOCFLOW_TOKEN`. |
| `X-OOA-Tenant` | The tenant id: lower case, `^[a-z0-9][a-z0-9_-]{0,62}$`. Always `default` on premises. Anything else gets `400 BAD_TENANT`. |
| `X-OOA-User` | The person's display name, URL-encoded UTF-8. Only used for "reviewed by" and audit. |
| `Content-Type`, `Accept` | Passed through from the browser. No other request header is forwarded. |

Only `Content-Type` and `Content-Disposition` come back from DocFlow's response. A wrong token gets `401`, which
the Command Center shows as "DocFlow refused the Command Center's token".

## Routes the platform calls (under `/api/docs/v1/`)

The console's screens (`platform/ui/src/docflow/http.ts`), through the proxy:

| Method | Route | Used for |
|---|---|---|
| GET | `df/overview` | Home page counts |
| GET | `df/flows` | Process list (also read by the email poller and "Send document to process") |
| POST | `df/flows` | New process |
| GET, PUT | `df/flows/{id}` | Open and save a process |
| POST | `df/flows/{id}/live?live=true\|false` | Go live, back to draft |
| GET | `df/flows/{id}/documents?limit=200&fields=true` | A process's documents (`{id}` may be `all`) |
| POST | `df/flows/{id}/documents` | Add documents: multipart field `files`. Query `source`, `requireLive`, `wait` |
| GET | `df/documents/{id}` | One document with fields, confidence, flags |
| POST | `df/documents/{id}/review` | Approve or reject (`action`) |
| POST | `df/documents/{id}/reclassify` | Change the document type |
| GET | `df/review` | Review queue (also read every 20 s for Task inbox tasks) |
| GET | `df/doctypes`, `df/library` | Document types, built-in library |
| POST | `df/doctypes` | New document type |
| GET, PUT, DELETE | `df/doctypes/{id}` | Open, save, delete a document type |
| POST | `df/doctypes/suggest` | Teach from samples: multipart field `files` |
| GET, POST | `df/reftables` | Reference data list, upload |
| GET, DELETE | `df/reftables/{id}` | Reference table rows (`?limit=`), delete |
| POST | `df/test` | Test lab: multipart `file`, `docType`, `engines` (comma list) |
| GET | `providers` | Engines and whether each is ready |
| GET | `costs` | Prices and the cloud opt-in |
| GET | `usage?groupBy=provider_id` | Pages and credits per engine |
| GET, PUT | `admin/settings` | Engine settings and routing (`{"values": {...}}`) |
| POST | `admin/providers/{id}/test` | Test one engine |
| GET | `documents/{id}/result` | OCR text and which engines read it |

The proxy also allows `documents/{id}/page/{n}.png` and `documents/{id}/file`, and any other `df/...` route. It never
forwards `admin/update` or `admin/installer.bat`.

### Response fields the platform reads

- **`df/flows`, `df/flows/{id}`**: `id`, `name`, `description`, `folder`, `docTypes`, `status` (`draft`/`live`),
  `isInbox`, `version`, `classify`, `engine`, `review.fieldConfidence`, `review.slaHours`, `rules[]`,
  `deliveries[]` (`id`, `type`, `name`, `enabled`, `on`, `params`), `intake.inputFolder`, `intake.pollSeconds`,
  `intake.email`, `updatedAt`, and optionally `stats`, `docTypeNames`. The built-in inbox has id `smart-inbox`.
- **`df/review`**: a list of documents, each with `id`, `file_name`, `created_at`, `flowId`, `flowName`,
  `doctypeName`, `slaHours`, `flags[].message`.
- **`df/documents/{id}`**: `id`, `status`, `flowId`, `fields`, `confidence`, `flags`, `error`, `file_name`,
  `created_at`, `source`, `parent_id`, `doctype_id`, `validated_by`.
- **`df/flows/{id}/documents`** (rows): `id`, `file_name`, `created_at`, `status`, `instance_id`, `source`,
  `parent_id`, `doctype_id`, `flags`, `validated_by`, and with `fields=true` also `fields`, `confidence`, `pages`,
  `engines`.
- **`POST df/flows/{id}/documents`**: `{"documents": [{"fileName", "documentId", "status", ...}]}`.
- **Document statuses**: `queued`, `processing`, `success`, `validation`, `validated`, `invalid`, `failed`, `split`.

### What the Command Center adds to a process before saving it

- **"Start a bot" delivery**: the Command Center checks the person may start that agent, then sets
  `params.platform = true`, `params.requestedBy = <username>`, `params.bot = <path>.flow.json`, and removes
  `params.url` and `params.authHeader`. DocFlow must accept a `start_bot` delivery with `platform: true` and no URL.
- **Email inbox** (`intake.email`): the Command Center reads the mailbox itself and only stores the settings here.
  It adds `requestedBy`. Secrets are vault references, never passwords.

## Calls from DocFlow to the Command Center

Both go to `DOCAUTO_PLATFORM_URL` (in SaaS `{tenant}` in it is replaced by the tenant). They carry
`Authorization: Bearer <DOCAUTO_API_TOKEN>` (the same token) and `X-OOA-Tenant`.

| Call | Body | Command Center does |
|---|---|---|
| `POST /api/docflow-callback/start-bot` | `{"bot", "pool", "requestedBy", "input": <document payload>}`. The payload has `documentId`, `flowId`, `flowName`, `status`, `fileName`, `fields`, ... | Starts the agent as `requestedBy`, filling its inputs `document`, `document_id`, `process`, `status`, `file_name` and any field by name |
| `POST /api/docflow-callback/events` | `{"event", "event_id", "document_id", "process_id", "process", "status", "file_name", "doc_type", "fields", "at"}` | Not built yet: see below |

## Known gaps (2026-11-07)

1. **Events endpoint missing on the platform.** DocFlow posts document events to `/api/docflow-callback/events`. The
   Command Center has no such route yet and answers 404, which DocFlow treats as final (no retries). Nothing breaks:
   the Command Center reads the review queue every 20 s instead. Adding the endpoint is a later phase.
2. **Open API without a token in standalone installs.** If `DOCAUTO_API_TOKEN` is not set, the API accepts every
   request. When the platform runs DocFlow it sets `DOCAUTO_MANAGED=true`. In that mode DocFlow refuses to start
   without the token, does not update itself, and keeps its install folder out of `admin/version`.
