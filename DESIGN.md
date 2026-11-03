# Document Automation Component: Design

**Product:** Oneoffice RPA platform · **Component:** Document Automation (OCR + IDP)
**Version:** 1.0 · **Date:** 2026-11-01 · **Status:** Built as a standalone component, ready to plug into the main RPA tool

This document explains *what* the component is, *why* it is shaped this way (benchmarked against UiPath, Automation Anywhere, SS&C Blue Prism and Microsoft Power Automate), and *how* the main RPA tool plugs into it. The code in this repository implements everything marked **Built**.

Companion documents: `README.md` (run and integrate), `claude/Document_Automation_OCR_Integration_Spec.md` in the OCR Solutions project (full contract spec), and `RPA_Platform_Deep_Comparison.docx` (competitor source).

---

## 1. What other vendors do (from the comparison document)

All four leaders treat document processing as a **separate product inside the platform**, with its own engine choice, human validation and consumption billing. None depends on a single OCR engine.

### 1.1 Product map

| | UiPath | Automation Anywhere | SS&C Blue Prism | Power Automate |
|---|---|---|---|---|
| IDP product | Document Understanding / IXP | Document Automation (successor of IQ Bot) | Decipher IDP | AI Builder document processing / Azure AI Document Intelligence |
| How it plugs in | Activity packages in Studio (Digitize, Classify, Extract, Validate, Train) | Native IDP inside the same Control Room; "Document Automation" action package | Separate Decipher server with its own database; called via DX skills / VBOs | AI Builder actions inside flows |
| OCR engines | UiPath Document OCR, Microsoft, Tesseract, Google, Azure, OmniPage, ABBYY | Tesseract, ABBYY, Google, Microsoft (OCR action); built-in engines for IDP | Built-in OCR (Tesseract-based) for screen regions; ABBYY and others via Digital Exchange | Tesseract, Windows OCR; Azure AI via AI Builder |
| Classifiers | Keyword, Intelligent Keyword, ML, generative | Learning-instance document types, gen AI classification | Trainable classification | Custom classification models |
| Extractors | Regex, Form, Intelligent Form, ML, Generative | Pre-trained and custom models, gen AI extraction | ML-based capture, gen AI options | Prebuilt and custom models |
| Human validation | Validation Station (attended) or Action Center tasks | Validator in Control Room / AARI | Verification UI (Interact) | AI Builder human review / Approvals |
| Retraining | Automatic via AI Center pipelines | Feedback improves learning instances | Training from verified data | Retrain custom models |
| Prebuilt models | Invoices, receipts, POs, IDs, passports, utility bills, tax forms, remittances, delivery notes, cheques, ACORD | Invoices, POs, receipts, IDs + user-defined with gen AI | Invoice starter models + user-trained | Invoices, receipts, IDs, business cards, contracts |
| LLM choice | AI Trust Layer: OpenAI, Azure OpenAI, Anthropic, Gemini, Bedrock, bring-your-own | OpenAI, Azure OpenAI, Bedrock, Vertex, custom endpoints | Major providers via DX connectors | Azure OpenAI / Microsoft-hosted |
| Billing | AI Units / Platform Units | Page or document volume + AI credits | Page or document volume | AI Builder credits |

### 1.2 Common pipeline (all vendors)

`Ingest → Digitize (OCR) → Classify → Extract → Validate (thresholds, rules, human) → Train / improve → Export`

### 1.3 What AA does specifically (the model we follow)

The user asked for an Automation Anywhere-like experience. AA's choices that we copy:

- **IDP lives inside the platform, not as a bolt-on.** It runs in the same control plane, with its own action package: *Upload documents, Extract data, Get extraction results, Send to validation, Download results*.
- **A separate, simpler OCR action** for screen capture (*Capture area, Capture window, Capture image by path*) where you pick the engine (Tesseract, ABBYY, Google, Microsoft).
- **Generative AI extraction** for user-defined document types, so customers are not limited to prebuilt models.
- **A validator queue** for humans, plus metered consumption (pages + AI credits).

### 1.4 Gaps we exploit

From the comparison's caveats and gap lists:

1. **IDP and AI features are often cloud-only or cloud-first** at the big vendors. We give **on-prem parity**: the same component and the same options, including self-hosted AI.
2. **Arabic and bilingual documents** are not a headline strength anywhere. We route Arabic to strong engines and treat RTL properly.
3. **Engine choice is per-activity**, not policy-driven. We add **deployment profiles and residency rules** that the router enforces.
4. **Billing is opaque** (AI units vs credits vs pages). We use **one credit meter** across free, bring-your-own-key and resold engines.

---

## 2. Design principles

1. **A separate component behind a stable contract.** The RPA tool talks to it only through REST (or in-process Python) and the `DocumentResult` schema. Engines can be swapped without touching bots.
2. **Three explicit option packs.** Each pack is a folder of self-contained connectors (manifest + code) that is enabled, licensed and shipped separately.
3. **Policy before engine.** The deployment profile decides what may leave the network, *before* any engine is chosen.
4. **Template-free by default.** Generic OCR, built-in rules, or LLM extraction against a user-defined field schema, so it handles *any* document from day one.
5. **Human in the loop by design.** Confidence + validation rules decide auto-approve vs review.
6. **Measure everything.** Every engine call is metered (pages, credits, outbound or not).

---

## 3. The three option packs (Built)

```
options/
├── tier1_opensource/        OPTION 1 - free, bundled, offline
│   ├── tesseract/           Built  - screen OCR, documents, searchable PDF, Arabic
│   └── paddleocr/           Built  - optional install (pip install paddleocr paddlepaddle)
├── tier2_cloud/             OPTION 2 - licensed / cloud connectors (BYOK or resold)
│   ├── azure_di/            Built  - REST, read + layout + tables + key-values + prebuilt models
│   ├── google_docai/        Built  - Document OCR + Form Parser processors
│   ├── aws_textract/        Built  - sync API page by page (tables, forms)
│   └── abbyy_cloud/         Template - product depends on ABBYY agreement (Vantage / FRE / Cloud)
└── tier3_ai/                OPTION 3 - AI extraction
    ├── llm_openai_compatible/  Built - "llm-cloud": any OpenAI-compatible API or gateway
    └── llm_selfhosted/         Built - "llm-local": same code, on-prem server (vLLM, Ollama...)
```

Core engines that ship with the component and are always available:

- **`pdf-text`**: reads the text layer of digital PDFs (no OCR, exact, 0 credits).
- **`builtin-rules`**: offline extractor and classifier (key-values → label matching with OCR-error tolerance → regex), no AI needed.

Turning a pack on or off is configuration only:

```yaml
option_packs:
  tier1_opensource: true
  tier2_cloud: true
  tier3_ai: true
```

A connector that is enabled but not configured (missing key, SDK not installed) is listed as *unavailable* with the reason, and the router skips it.

---

## 4. Architecture (Built)

```
 Main RPA tool (designer + bot runner, Java/AA-style)
   │  action package: integration/action-package.json
   │  Java client:    clients/java (DocAutoClient)
   ▼  REST /api/docs/v1
┌──────────────────────────────────────────────────────────────────┐
│ Document Service (docauto/)                                      │
│                                                                  │
│  preprocess.py   ingest PDF/images, render 300 dpi, deskew,      │
│                  upscale screenshots, detect digital-PDF pages   │
│  policy.py       deployment profile + allow/deny + residency     │
│  router.py       default → cheapest → configured fallbacks;      │
│                  retry with backoff; low-confidence escalation   │
│  registry.py     discovers options/<pack>/<engine>/manifest.json │
│  pipeline.py     digitize → classify → extract → score → save    │
│  extraction.py   built-in rules extractor + safe rule evaluator  │
│  scoring.py      field checks, cross-field rules, review decision│
│  store.py        SQLite: jobs, documents, schemas, usage meter   │
│  api/app.py      REST API + Validation Station (/validation)     │
└──────────────────────────────────────────────────────────────────┘
```

### 4.1 Processing flow

1. **Ingest:** detect type, render PDF pages, deskew scans, upscale screenshots, and mark pages that have a usable text layer.
2. **Digitize:** if every page is digital, use `pdf-text` (free). Otherwise the router picks an OCR engine allowed by policy that has the requested capabilities (e.g. `tables`) and languages.
3. **Escalate (optional):** if mean OCR confidence < threshold and `escalate_to` is configured, re-run with the premium engine and keep the better result.
4. **Classify (optional):** against the caller's labels (rules or LLM).
5. **Extract (optional):** against a `FieldSchema` (rules or LLM).
6. **Score:** per-field checks (required, pattern, enum) → cross-field rules (`abs(subtotal + vat_amount - total) < 0.05`). Passing rules raise confidence for the fields they reference. Anything below threshold sends the document to review.
7. **Save + meter:** `DocumentResult` stored; every engine call written to the usage table with pages, credits and whether it went outbound.

### 4.2 Deployment profiles (Built)

| Profile | What may run | Typical customer |
|---|---|---|
| `onprem_airgapped` | Only engines with `runsLocally: true` (Tesseract, PaddleOCR, pdf-text, llm-local, ABBYY FRE) | Government, banks |
| `onprem_hybrid` | Local + outbound engines on the allow list and in allowed regions | Most enterprises |
| `cloud_saas` | Everything, subject to tenant allow/deny and residency | SaaS tenants |

An explicit request for a forbidden engine returns `403 POLICY_DENIED`. Data never silently leaves the network.

### 4.3 AI extraction safety (Built)

- The prompt gives the model OCR lines tagged with ids (`[p1.l3] ...`), and the model must cite them.
- Values that cannot be found in the OCR text get confidence halved and the error *"value not found in OCR text (possible hallucination)"*, which forces review.
- JSON mode, one automatic repair retry, and typed coercion (dates → ISO, amounts → numbers, Arabic-Indic digits supported).

---

## 5. How the main RPA tool plugs in

### 5.1 Integration surfaces

| Surface | File | What the host does |
|---|---|---|
| REST API | `docauto/api/app.py` | Calls `/api/docs/v1/...`; replace `auth()` with platform token validation |
| Designer actions | `integration/action-package.json` | Registers 9 actions with inputs/outputs mapped to REST calls (AA-style package) |
| Java client | `clients/java/.../DocAutoClient.java` | Zero-dependency client for the Java bot runner |
| Web UI | `/ui` (`?embed=1#/option/<pack>` per-page embedding) | Option 1/2/3 configuration and testing, process, compare, document types, review, usage, settings; host can embed pages in its web view |
| Validation Station | `/validation` (`?doc=<id>` deep link) | Embed in the control room, or create a human task that links to it |
| Admin | `config/document-service.yaml` | Map to the host's admin UI: profile, packs, engine keys, thresholds |
| Usage | `/api/docs/v1/usage` | Feed the host's licensing / billing dashboard |

### 5.2 Designer actions (AA naming)

| Action | REST call |
|---|---|
| Upload documents | `POST /documents` (multipart file + options) |
| Get extraction results | `GET /jobs/{id}` (poll) |
| Extract data | `GET /documents/{id}/fields` |
| Read document text | `GET /documents/{id}/result` |
| Send to validation | waits on `reviewStatus` (or host human task) |
| Download results | `GET /documents/{id}/export?format=xlsx\|csv\|json` |
| Screen OCR | `POST /ocr/screen` |
| Find text on screen | `POST /ocr/screen` + locate phrase → click coordinates |
| Make searchable PDF | `POST /pdf/searchable` |

### 5.3 Plug-in checklist for the main tool team

1. Deploy the container (`docker compose up`) next to the control room, or install on a Windows server for on-prem robots.
2. Point the host's settings `docauto.baseUrl` / `docauto.apiToken` at it.
3. Load `integration/action-package.json` into the action/package registry; implement the generic "REST-backed action" runner once.
4. Replace `auth()` in `api/app.py` with platform token validation; map tenants.
5. Hook "Send to validation" into the host's human-task inbox (deep link `/validation?doc=<id>`).
6. Map `config/document-service.yaml` into the admin console (profile, option packs, engine keys, thresholds).
7. Optionally swap `store.py` (SQLite) for the platform database.

---

## 6. Commercial model hooks

- Every engine manifest carries `creditCostPerPage`; usage rows record pages, credits, option pack and `runs_locally`.
- **Bring-your-own-key:** customer keys go in `providers.settings.<id>.credentials`; credits are informational.
- **Resold:** platform-owned keys; bill from the usage table.
- Suggested editions: **Standard** = Option 1; **Professional** = + Option 2 and the Validation Station; **Enterprise** = + Option 3, residency profiles, connector SDK.

---

## 7. Verified behaviour (this build)

The test suite runs 35 tests, all passing, with real Tesseract 5.3 (eng + ara):

- **Contract tests:** Azure, Textract and Google Document AI mappers against vendor-shaped fixtures; Tesseract on a real screenshot; normalized bounding boxes and confidences.
- **Router/policy:** air-gapped blocks cloud engines; residency and deny lists; default → cheapest → fallback ordering; retry then fallback with provenance.
- **Extraction:** most specific label wins, word boundaries ("Total" ≠ "Subtotal"), OCR-error tolerance ("Subtota]"), Arabic labels and digits, safe rule evaluation rejects code injection.
- **AI option (mocked HTTP):** grounding, hallucination guard, JSON repair, auth error mapping.
- **End-to-end API:** bilingual scanned invoice → all 6 fields correct, 2 low-confidence fields routed to review → review completed; digital PDF → no OCR, auto-approved; screen OCR; searchable PDF; 403 under the air-gapped profile.
- **Java client:** compiled with JDK 21 and run against the live service (upload → wait → fields → CSV export).

---

## 8. Roadmap from here

| Next | Why |
|---|---|
| Configure Azure DI + one LLM endpoint and run the golden set | Real accuracy numbers for Option 2/3; tune thresholds |
| Arabic golden set (e-invoices, CR, IDs, bank statements) | Pick the default Arabic engine from evidence |
| PaddleOCR install in the image (`WITH_PADDLE=true`) and benchmark vs Tesseract | Better offline accuracy |
| Complete the ABBYY connector once the product/agreement is chosen | Premium on-prem option |
| Field-schema builder UI in the designer | Business users define document types without JSON |
| Feedback loop: store corrections as few-shot examples per schema | Improves LLM extraction per tenant (AA/UiPath retraining parity) |
| Async Textract (S3) and batch endpoints | High-volume workloads |
| Signed third-party connector packages + marketplace | Ecosystem (Bot Store / DX equivalent) |

*Competitor details come from the attached comparison (knowledge as of mid-2026); verify current product names, engine lists and cloud-only features before external use.*

---

## 9. Oneoffice DocFlow (v3): our own Document Automation model

Competitors were used as inspiration only. DocFlow is designed around **business processes**, not one configuration per document type, and adds capabilities the big three sell separately or not at all.

### 9.1 Concepts

| Concept | What it is | Why it is better for our customers |
|---|---|---|
| **Doc Type** | Reusable definition: fields and tables, English/Arabic labels, recognition keywords, consistency checks, labels learned from reviewers | Define once, use in many flows; the Smart Inbox recognises every type |
| **Doc Flow** | A process in six stages: **Intake → Classify & split → Extract → Rules & lookups → Review → Deliver**. One flow can accept several doc types | Matches how customers think ("AP invoices to ERP"), not how an OCR engine works |
| **Smart Inbox** | Built-in flow that accepts *any* document: pages are recognised against all doc types, mixed scanned batches are split, each part is extracted with its own type | Business users just drop files; no setup per document |
| **Review Desk** | One queue for everything needing a person (low confidence, failed checks, rule flags, unknown type) with **SLA**, overdue highlighting and alert webhook | Operations can staff and measure review work |
| **Reference Data** | Upload CSV/Excel master data (suppliers, POs, cost centres) | Lookups validate and enrich extracted data |
| **Teach from samples** | Upload 1–3 examples: DocFlow proposes the type, fields, data types, labels and keywords (rules-based offline; AI when Option 3 is configured) | New document types in minutes, without field-by-field setup |

### 9.2 Upgrades beyond typical RPA IDP

1. **AI-assisted setup:** teach from samples, merged with the built-in library when the type is recognised.
2. **Classify & split:** page-level recognition with keyword and label scoring, start-of-document anchors, OCR is reused for the parts (no second read), and each part is saved as its own file.
3. **Business rules & lookups:** duplicate detection, reference-data lookup with enrichment, KSA VAT (ZATCA) checks, amount thresholds and approvals, required fields, custom expressions. Each rule can send the document to review, reject it, or warn.
4. **Delivery & integrations:** webhook, **start a bot** in the Oneoffice RPA tool with the data as input, Excel register (one sheet per type), folder. Every delivery is logged, retried once, and can be test-sent.
5. **Lifecycle:** Draft → **Live**; versions on every change; documents keep the flow version they ran with.
6. **Learning:** reviewer corrections teach the doc type new labels (shared across all flows using it) and guide AI extraction.

### 9.3 Statuses

`queued → processing → success` (auto-processed) · `validation` (in Review Desk) → `validated` / `invalid` (rejected) · `failed` · `split` (batch parent with child documents).

### 9.4 UI map

One View (KPIs, flows, activity) · One Inbox · OneFlow (list + six-stage builder, documents, dashboard, delivery log, connect a bot) · One Library (doc types, ✨ Teach One, learned labels) · One Desk · One Source · OneEngine (OneCore, OneConnect, OneMind, One-click test, One-to-one compare) · OneMeter · Settings.

### 9.5 API (base `/api/docs/v1/df`)

`GET /library` · `GET|POST /doctypes` · `GET|PUT|DELETE /doctypes/{id}` · `POST /doctypes/suggest` · `GET /doctypes/{id}/learning` · `GET|POST /flows` · `GET|PUT|DELETE /flows/{id}` · `POST /flows/{id}/live?live=true|false` · `POST|GET /flows/{id}/documents` · `GET /flows/{id}/stats|export|deliveries` · `POST /flows/{id}/deliveries/{d}/test` · `POST /flows/{id}/intake/scan` · `GET /documents/{id}` · `POST /documents/{id}/review|reclassify` · `GET /review` · `GET /review/next` · `GET /overview` · `GET|POST /reftables` · `GET|DELETE /reftables/{id}`

### 9.6 Bot package "DocFlow"

**Send document to flow** (a live flow or `smart-inbox`) → **Wait for document** (optionally until reviewed; returns children for split batches) → **Get document result** → **Export flow results**. Java client: `sendToFlow`, `waitForDocument`, `getDocumentResult`, `exportFlow`. Flows can also push results to a bot through the *Start a bot* delivery.

Learning instances from v2 are migrated automatically on first start, each becoming a doc type plus a flow.

---

## 10. OneOffice brand and "One" naming (v3.1)

The UI uses the OneOffice Automation logo (two interlocking rings) and brand colours. Every menu item carries the **One** prefix so the component reads as part of the OneOffice family.

### 10.1 Brand tokens

| Token | Value | Use |
|---|---|---|
| Brand blue | `#07649C` (dark `#004F80`, light `#1C77B0`) | Primary actions, OneCore accent, "One" prefix |
| Brand violet | `#6B40C2` (light `#8458D8`) | OneConnect accent, gradient end |
| Brand gradient | `linear-gradient(90deg, #06598B, #6B40C2)` | Primary buttons, active menu bar, wordmark |
| OneMind accent | `#B0307E` | AI option |
| Font | Poppins (Google Fonts), system fallback | All UI text |

Assets are served from `/ui/brand/`: `ooa-logo.svg` (full logo), `ooa-mark.svg` (rings only), `ooa-logo.png`, `favicon.png` (also at `/favicon.ico`).

### 10.2 Naming map

| Old name | OneOffice name | Route |
|---|---|---|
| Home | **One View** | `#/home` |
| Smart Inbox | **One Inbox** (flow id stays `smart-inbox`) | `#/inbox` |
| Doc Flows | **OneFlow** | `#/flows` |
| Doc Types | **One Library** | `#/doctypes` |
| Teach from samples | **✨ Teach One** | `#/teach` |
| Review Desk | **One Desk** | `#/review` |
| Reference Data | **One Source** | `#/refdata` |
| Engines section | **OneEngine** | — |
| Option 1 · Open source | **OneCore** | `#/option/tier1_opensource` |
| Option 2 · Cloud & licensed | **OneConnect** | `#/option/tier2_cloud` |
| Option 3 · AI extraction | **OneMind** | `#/option/tier3_ai` |
| Quick test / Process | **One-click test** | `#/process` |
| Compare engines | **One-to-one compare** | `#/compare` |
| Usage | **OneMeter** | `#/usage` |

API paths, ids and pack folder names are unchanged, so bots and the action package keep working. Existing installs rename the "Smart Inbox" flow to "One Inbox" automatically on start.

---

## 11. Distribution: install and update from GitHub (v3.1)

The component is kept in a private GitHub repository; PCs install and update from it (details in `GITHUB.md`).

- **Installer:** `windows/Install-OneOffice-DocFlow.bat` (downloadable from Settings > Updates & install with the repository filled in, and attached to each GitHub release). It fetches `windows/get-docflow.ps1` through the GitHub API with a read-only fine-grained token, stored per Windows user with DPAPI.
- **Updater:** `get-docflow.ps1` checks the latest commit/release, stops the service, backs up code + database to `backups/` (last 5), mirrors code folders, keeps `config/local.env`, `data/`, `tessdata/`, `.venv/`, reinstalls packages only when `requirements.txt` changed, restarts. A git working copy is updated with `git pull`. Channel via `DOCFLOW_REF`: `main`, `release`, or a tag.
- **UI/API:** `GET /api/docs/v1/admin/version`, `POST /admin/update` (Windows: opens the updater window), `GET /admin/installer.bat`.
- **CI:** Linux tests + a real install on a clean Windows runner through the same installer; tag `vX.Y.Z` (matching `VERSION`) publishes a release.
