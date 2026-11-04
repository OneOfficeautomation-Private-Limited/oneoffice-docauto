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

DocFlow has no screens of its own. People use it in the **OOA Command Center**, under **AI > Document Automation**:
Inbox (add documents), Processes (the six stages: Intake, Classify & split, Extract, Checks, Review, Deliver), Document
types (with Teach from samples), Reference data, Engines (with Test and Configure) and Test lab. Documents that need a
person arrive in the **Task inbox**. Connecting DocFlow to the Command Center (token, address) is described in the
platform's `docs/DOCUMENT_AUTOMATION.md` and in the README ("With the OOA platform").

## 2b. Configure engines

In the Command Center: **AI > Document Automation > Engines**, then **Configure** and **Test** on each engine. On a
server without the Command Center, use the command-line wizard below.

### Command-line wizard

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

Double-click **`windows\start.bat`**. Keep the window open while you use the service, and press Ctrl+C to stop it.

| Page | URL |
|---|---|
| Screens | OOA Command Center: AI > Document Automation |
| API (try every call) | http://localhost:8085/docs |
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
