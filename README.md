# OneOffice DocFlow: Document Automation component (standalone)

Pluggable OCR + intelligent document processing for the Oneoffice RPA platform, built to be developed and tested on its own and then plugged into the main tool.

It offers three separate engine options:

| Option pack | Engines | Runs |
|---|---|---|
| **OneCore · Open source** (`options/tier1_opensource`) | Tesseract, PaddleOCR | Offline, free |
| **OneConnect · Licensed / cloud** (`options/tier2_cloud`) | Azure AI Document Intelligence, Google Document AI, AWS Textract, ABBYY (template) | Bring-your-own-key or resold |
| **OneMind · AI extraction** (`options/tier3_ai`) | Any OpenAI-compatible LLM API (`llm-cloud`) or a self-hosted model (`llm-local`) | Cloud or on-prem |

The core component also ships with a digital-PDF text reader (`pdf-text`) and an offline rules extractor (`builtin-rules`).

See **DESIGN.md** for the architecture and the competitor benchmark.

## Quick start

**Windows (recommended):** install and update from GitHub with one double-click; see `GITHUB.md`.

**Windows from a folder:** see `INSTALL-WINDOWS.md` (double-click `windows\install.bat`, then `start.bat`).

**Linux / macOS:**

```bash
pip install -r requirements.txt          # plus: tesseract-ocr with eng + ara language data
python samples/make_samples.py           # synthetic invoice scan, digital PDF, screenshot
pytest -q                                # runs on a throw-away embedded PostgreSQL (or DOCAUTO_TEST_DATABASE_URL)
DOCAUTO_CONFIG=config/document-service.yaml uvicorn docauto.api.app:app --port 8085
```

**Storage:** PostgreSQL only. Without `DOCAUTO_DATABASE_URL` DocFlow starts an embedded PostgreSQL 16 in
`<data dir>/postgres` (nothing to install); set `DOCAUTO_DATABASE_URL=postgresql://user:pass@host:5432/db` to use your
own server. Each tenant has its own schema; stored documents are encrypted (`DOCAUTO_FILE_KEY`, else a key generated
into `<data dir>/file.key`: back it up). An install from before v4 (SQLite `data/docauto.db`) is copied over with
`python -m docauto.migrate_sqlite data` (`windows\start.ps1` does it on its own). Backups: `python -m docauto.backup
data backup.dump`. Details: DESIGN.md §12.

DocFlow is a service without screens of its own: people use it through the OOA Command Center
(AI > Document Automation: inbox, processes, document types, reference data, engines, Test lab, and reviews in the
Task inbox). See "With the OOA platform" below.

- http://localhost:8085/docs: interactive API
- http://localhost:8085/: where to find the screens, and the version

Or with Docker (DocFlow + a PostgreSQL 16 service):

```bash
DOCFLOW_DB_PASSWORD=... docker compose up --build           # Option 1 only by default
docker compose build --build-arg WITH_PADDLE=true           # add PaddleOCR
```

## Try it

```bash
# register a document type (field schema)
curl -X POST localhost:8085/api/docs/v1/schemas -H 'Content-Type: application/json' -d @samples/invoice_schema.json

# process a scanned bilingual invoice and wait for the result
curl -F file=@samples/invoice_scan.png \
     -F 'options={"schemaId":"supplier-invoice","languages":["en","ar"],"classifyLabels":["invoice","contract"]}' \
     'localhost:8085/api/docs/v1/documents?wait=true'

curl localhost:8085/api/docs/v1/documents/<documentId>/fields
curl -F file=@samples/screen.png localhost:8085/api/docs/v1/ocr/screen
curl localhost:8085/api/docs/v1/providers        # engines, option packs, policy status
curl localhost:8085/api/docs/v1/usage            # pages / credits per engine
```

## Measure accuracy (benchmark)

Runs a labelled test set through one or more engines and scores it against the targets in the OCR strategy plan: character accuracy, field accuracy, straight-through rate, cloud share and turnaround, plus documents auto-approved with a wrong field. Results are broken down by document type (clean, scan, fax, photo, pdf) and by field.

```bash
python samples/make_benchmark_set.py                 # 10 synthetic invoices x 5 variants -> benchmark-data/synthetic
python -m docauto.benchmark benchmark-data/synthetic --engines tesseract,auto
python -m docauto.benchmark benchmark-data/synthetic --engines tesseract,azure-di --targets cloud
```

The report (`report.md` + `results.json`) goes to `benchmark-data/reports/`. To test real documents, put each file next to a `<name>.truth.json` with the correct values (see `docauto/benchmark.py`) in a folder under `benchmark-data/`, which Git ignores: the repository is public, so client documents must never be committed.

## Enable Option 2 / Option 3

Set the environment variables referenced in `config/document-service.yaml` and restart:

| Engine | Variables |
|---|---|
| Azure DI | `AZURE_DI_ENDPOINT`, `AZURE_DI_KEY`, `AZURE_DI_REGION` |
| AWS Textract | `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (+ `pip install boto3`) |
| Google Document AI | `GCP_PROJECT`, `DOCAI_LOCATION`, `DOCAI_OCR_PROCESSOR`, `DOCAI_FORM_PROCESSOR`, `GOOGLE_APPLICATION_CREDENTIALS` (+ `pip install google-cloud-documentai`) |
| LLM (cloud) | `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`, `LLM_REGION` |
| Claude | `CLAUDE_API_KEY`, `CLAUDE_MODEL` (default `claude-haiku-5-5`), `CLAUDE_ESCALATION_MODEL` (default `claude-sonnet-5-5`, `none` = off) (+ `pip install anthropic`) |
| LLM (self-hosted) | `LOCAL_LLM_BASE_URL`, `LOCAL_LLM_MODEL` |

Then make them the defaults, e.g. `defaults.document_ocr: azure-di`, `defaults.extraction: llm-cloud`. Profiles for air-gapped and SaaS deployments are in `config/profiles/`.

## Plug into the main RPA tool

- **Designer actions:** `integration/action-package.json` (AA-style: Upload documents, Extract data, Get extraction results, Send to validation, Download results, Screen OCR, Find text on screen, Make searchable PDF).
- **Java client:** `clients/java` (`DocAutoClient`, zero dependencies, Java 11+). Run `Example.java` against a live service.
- **Python in-process:** `from docauto import DocumentService, ProcessOptions`.
- Checklist: DESIGN.md §5.3.

### With the OOA platform (AI > Document Automation)

The OOA Command Center shows DocFlow's screens and forwards to it; set in `config/local.env`:

| Setting | Value |
| --- | --- |
| `DOCAUTO_API_TOKEN` | the service token; the same value goes in the Command Center's vault (Settings > Document Automation) |
| `DOCAUTO_PLATFORM_URL` | the Command Center's address, for "Start a bot" deliveries and document events (`{tenant}` is replaced in SaaS) |
| `DOCAUTO_MANAGED` | `true` when the OOA platform runs DocFlow (its installer, containers or SaaS): it will not start without `DOCAUTO_API_TOKEN`, does not update itself (the platform upgrades it), and `admin/version` does not show the install folder |

With `DOCAUTO_PLATFORM_URL` set, DocFlow also tells the Command Center when a document in a process is processed,
needs review, is approved or rejected, has a failed delivery, or is overdue for review (DESIGN.md §9.7). Events and
deliveries go through a durable outbox: they survive restarts, are retried with backoff and sent once
(`GET /api/docs/v1/df/outbox`).

### Production settings

| Variable | Default | What it does |
| --- | --- | --- |
| `DOCAUTO_DATABASE_URL` | empty = embedded PostgreSQL in `<data dir>/postgres` | the PostgreSQL server (UTF8) |
| `DOCAUTO_FILE_KEY` | empty = generated into `<data dir>/file.key` | Fernet key(s) for documents at rest; `new,old` rotates |
| `DOCAUTO_RETENTION_DAYS` | 30 | documents older than this are deleted daily; 0 = keep forever |
| `DOCAUTO_MASK_PII` | true | mask national IDs / Iqama, IBANs, phones, e-mails before cloud AI models (then no page images unless a process allows them) |
| `DOCAUTO_DB_POOL_SIZE` | 20 | database connections per server and process |
| `DOCAUTO_JOB_STALE_SECONDS` | 90 | a running job without heartbeat this long is taken over by another worker |
| `DOCAUTO_OUTBOX_MAX_ATTEMPTS` / `DOCAUTO_OUTBOX_BACKOFF` | 10 / 10 s | delivery and event retries (doubling, at most 15 min apart) |

### Test sets (quality gate)

Per document type, keep a set of documents with their correct values (reviewed documents from a process, uploads with
expected values, or the synthetic seed: `POST /api/docs/v1/df/testsets/seed`) and compare engine combinations on it
(`POST /df/testsets/{id}/runs` with `{"combos": [{"ocr": "auto", "extraction": "builtin-rules"}, {"ocr": "tesseract",
"extraction": "claude"}]}`): per-field accuracy, straight-through rate, wrongly auto-approved documents, cost and time
per page, kept as runs. DESIGN.md §12.7.

Each platform tenant gets its own data under `data/tenants/<tenant>`. The full set-up, permissions and folders are in
the platform's `docs/DOCUMENT_AUTOMATION.md`.

## Add a new engine (connector SDK)

1. Create `options/<pack>/<engine>/manifest.json` (copy an existing one: id, tier, kind, capabilities, languages, runsLocally, creditCostPerPage).
2. Implement `provider.py` with a class extending `OcrProvider` (`recognize`) or `ExtractionProvider` (`extract`, `classify`), mapping output to `DocumentResult` using `docauto.normalize` helpers.
3. Add a contract test like those in `tests/test_contract.py`. The registry discovers it automatically.

## Layout

```
docauto/            core service (models, policy, router, pipeline, extraction, scoring, store, API)
options/            the three option packs, one folder per engine
clients/java/       Java client for the host RPA tool
integration/        designer action package manifest
config/             service config + deployment profiles
samples/            synthetic test documents + example field schema
tests/              149 tests (contract, policy/router, extraction, grounding, e-invoices, AI option, escalation, events,
                    outbox, storage, PII masking, test sets, DocFlow, updates, benchmark, end-to-end API; the Tesseract
                    ones skip when it is not installed: on Windows they take TESSERACT_CMD from config/local.env)
windows/            install, start, update (get-docflow.ps1), shortcuts, installer .bat
.github/workflows/  CI (Linux tests + real Windows install) and Release
```
