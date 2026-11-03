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
pytest -q
DOCAUTO_CONFIG=config/document-service.yaml uvicorn docauto.api.app:app --port 8085
```

Then open:

- http://localhost:8085/ui: DocFlow with the OneOffice brand: One View (home), One Inbox, OneFlow (processes), One Library (doc types, ✨ Teach One), One Desk (review), One Source (master data), and the OneEngine pages: OneCore (open source), OneConnect (cloud & licensed), OneMind (AI), One-click test, One-to-one compare, OneMeter (usage), Settings

- http://localhost:8085/docs: interactive API
- http://localhost:8085/validation: Validation Station

Or with Docker:

```bash
docker compose up --build                                   # Option 1 only by default
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

## Enable Option 2 / Option 3

Set the environment variables referenced in `config/document-service.yaml` and restart:

| Engine | Variables |
|---|---|
| Azure DI | `AZURE_DI_ENDPOINT`, `AZURE_DI_KEY`, `AZURE_DI_REGION` |
| AWS Textract | `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (+ `pip install boto3`) |
| Google Document AI | `GCP_PROJECT`, `DOCAI_LOCATION`, `DOCAI_OCR_PROCESSOR`, `DOCAI_FORM_PROCESSOR`, `GOOGLE_APPLICATION_CREDENTIALS` (+ `pip install google-cloud-documentai`) |
| LLM (cloud) | `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`, `LLM_REGION` |
| LLM (self-hosted) | `LOCAL_LLM_BASE_URL`, `LOCAL_LLM_MODEL` |

Then make them the defaults, e.g. `defaults.document_ocr: azure-di`, `defaults.extraction: llm-cloud`. Profiles for air-gapped and SaaS deployments are in `config/profiles/`.

## Plug into the main RPA tool

- **Designer actions:** `integration/action-package.json` (AA-style: Upload documents, Extract data, Get extraction results, Send to validation, Download results, Screen OCR, Find text on screen, Make searchable PDF).
- **Java client:** `clients/java` (`DocAutoClient`, zero dependencies, Java 11+). Run `Example.java` against a live service.
- **Python in-process:** `from docauto import DocumentService, ProcessOptions`.
- Checklist: DESIGN.md §5.3.

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
tests/              54 tests (contract, policy/router, extraction, AI option, DocFlow, updates, end-to-end API)
windows/            install, start, update (get-docflow.ps1), shortcuts, installer .bat
.github/workflows/  CI (Linux tests + real Windows install) and Release
```
