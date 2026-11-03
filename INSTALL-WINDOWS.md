# Install and configure on Windows

> **Easiest way:** install and update from GitHub with one double-click. See **GITHUB.md**. The steps below are for installing from a downloaded folder.

Everything runs locally on your PC. The scripts are in the `windows` folder, and every `.bat` file can be double-clicked.

## 1. Install (one time, about 5 minutes)

Double-click **`windows\install.bat`**. It runs these steps:

1. Finds Python 3.10+ and installs Python 3.12 with `winget` if it's missing.
2. Finds Tesseract OCR and installs the UB Mannheim build with `winget` if it's missing. Windows may ask for admin approval.
3. Builds a local `tessdata` folder with English + Arabic language data.
4. Creates a private Python environment (`.venv`) and installs the packages.
5. Creates `config\local.env`, generates sample documents and runs the test suite (49 tests).

If `winget` isn't available, install these by hand, then run `install.bat` again:

- Python: https://www.python.org/downloads/ (tick "Add to PATH")
- Tesseract: https://github.com/UB-Mannheim/tesseract/wiki

## 2. Use OneOffice DocFlow

Open **http://localhost:8085/ui**.

1. **One Inbox:** drop any document, even one scanned PDF holding several documents. DocFlow recognises each part, splits it and extracts the fields.
2. **One Library** (doc types): the library has Invoice, Purchase order, Receipt, Bank statement and ID document. **✨ Teach One** creates a new type from 1–3 example files.
3. **One Source** (master data): upload your supplier list (CSV or Excel) to use in lookup rules.
4. **OneFlow → + New flow:** set the six stages (Intake, Classify & split, Extract, Rules & lookups, Review, Deliver), create the flow, test it with **Upload documents**, then **Go live** so bots and watched folders can use it.
5. **One Desk:** approve or correct what needs a person; overdue items are highlighted.

## 2b. Configure engines (web UI)

Start the service (step 3) and open **http://localhost:8085/ui**. Everything below can be done there, with no restart:

| Page | What you do there |
|---|---|
| **OneCore** (open source) | Check Tesseract / PaddleOCR status, set the tesseract path, **Test engine** |
| **OneConnect** (cloud & licensed) | Enter Azure / AWS / Google / ABBYY settings and keys, **Save**, **Test engine**, enable/disable each engine |
| **OneMind** (AI extraction) | Point to a cloud LLM API or a local model (Ollama/vLLM), **Test engine** |
| **One-click test** | Upload a file, pick engines, see fields with confidence, boxes on the page, text, tables, credits |
| **One-to-one compare** | Run one file through several engines side by side |
| **Document types** | Define the fields to extract (labels in English/Arabic, patterns, validation rules) |
| **One Desk / OneMeter / Settings** | Approve low-confidence fields, see pages and credits, change profile and defaults |

Each page can be embedded in the RPA tool on its own, without the side menu: `http://localhost:8085/ui?embed=1#/option/tier2_cloud`.

### Alternative: command-line wizard

Double-click **`windows\configure.bat`** and answer the questions. Press Enter to keep the current value.

| Question | Recommendation for first run |
|---|---|
| Deployment profile | `onprem_hybrid` (or `onprem_airgapped` to guarantee nothing leaves the PC) |
| Option 1 - Open source | **y** |
| Option 2 - Licensed / cloud | y only if you have Azure / AWS / Google keys |
| Option 3 - AI extraction | y if you have an LLM API, or a local model in Ollama / vLLM |
| Default OCR engine | `tesseract` (later `azure-di` etc.) |
| Default extraction engine | `builtin-rules` (later `llm-cloud` or `llm-local`) |
| API token | leave blank for local testing; set one before the RPA tool connects |

The answers are saved in `config\local.env`, a plain text file you can also edit by hand. Keys stay on your PC.

**Free local AI option:** install Ollama, pull a model, then in `configure.bat` set the self-hosted LLM base URL to `http://localhost:11434/v1` with that model id, and set the default extraction to `llm-local`.

## 3. Start

Double-click **`windows\start.bat`**. The browser opens on the web UI. Keep the window open while you use the service, and press Ctrl+C to stop it.

| Page | URL |
|---|---|
| Web UI | http://localhost:8085/ui |
| API (try every call) | http://localhost:8085/docs |
| Validation Station | http://localhost:8085/validation |
| Engines and policy status | http://localhost:8085/api/docs/v1/providers |
| Usage per engine | http://localhost:8085/api/docs/v1/usage |

## 4. Check it works

With the service running, open a second PowerShell window in the app folder and run:

```powershell
powershell -ExecutionPolicy Bypass -File .\windows\test-service.ps1
```

You should see:

- the engine list: `tesseract`, `pdf-text` and `builtin-rules` available, and cloud engines unavailable until configured
- the six invoice fields extracted from `samples\invoice_scan.png`

Then open the Validation Station to approve the fields it flagged.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `running scripts is disabled` | Use the `.bat` files, or `powershell -ExecutionPolicy Bypass -File ...` |
| `tesseract not available` in providers | Check `TESSERACT_CMD` in `config\local.env` points to `tesseract.exe` |
| `language data missing: ['ara']` | Put `ara.traineddata` in the app's `tessdata` folder (download from github.com/tesseract-ocr/tessdata_fast) |
| Azure / LLM shows `available: false` | Read the `reason` column in the providers list; usually a missing key or URL in `local.env` |
| `POLICY_DENIED` | The profile blocks that engine (e.g. cloud engine under `onprem_airgapped`) |
| Port 8085 busy | Change `DOCAUTO_PORT` in `config\local.env` |
