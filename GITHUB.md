# OneOffice DocFlow on GitHub: install and update in one click

DocFlow lives in a **private GitHub repository**. Every PC installs and updates from it, so nobody has to download zips or copy folders again.

| Who | Does what | How often |
|---|---|---|
| You (developer PC) | Push changes with GitHub Desktop | Whenever the code changes |
| GitHub | Runs the tests and a full Windows install on a clean PC (CI) | Every push, automatically |
| Any PC | Double-clicks **Install-OneOffice-DocFlow.bat** | Once |
| Any PC | Clicks **Settings > Update from GitHub** (or Start menu > Update OneOffice DocFlow) | Whenever you publish a change |

## 1. One-time: put the code on GitHub (5 minutes, no commands)

1. Install **GitHub Desktop** (desktop.github.com) and sign in.
2. Double-click **`windows\setup-github-ci.bat`** once. It puts the automatic checks (CI) in `.github\workflows`.
3. **File > Add local repository** and choose `C:\Users\DELL\source\oneoffice-docauto`.
   It says the folder is not a repository yet: click **create a repository**, keep the name `oneoffice-docauto`, click **Create repository**.
   The `.gitignore` already keeps your settings, keys, database and `.venv` out of GitHub.
4. Click **Publish repository**, keep **Keep this code private** ticked, choose your account or the OneOffice organisation, click **Publish**.
5. On github.com open the repository > **Actions**: the first CI run starts by itself. Green = tests passed and DocFlow installed cleanly on a fresh Windows PC.

## 2. One-time per PC user: a read-only token

The repository is private, so each PC needs a token to download it. It is asked once and stored encrypted for that Windows user.

github.com > your picture > **Settings > Developer settings > Personal access tokens > Fine-grained tokens > Generate new token**

- Repository access: **Only select repositories** > `oneoffice-docauto`
- Permissions: **Contents: Read-only** (nothing else)
- Expiration: up to 1 year (create a new one when it expires; the updater asks again)

One token can be shared by your team's PCs. If someone leaves, delete the token on GitHub and make a new one.

## 3. Install on a new PC

Get **Install-OneOffice-DocFlow.bat**, which already contains your repository name:

- in DocFlow: **Settings > Updates & install > Installer for another PC**, or
- from the repository's **Releases** page (attached to each release).

Double-click it and paste the token. It installs Python and Tesseract if needed, downloads DocFlow to `%USERPROFILE%\OneOffice\DocFlow`, runs the tests, creates **OneOffice DocFlow** shortcuts on the Desktop and in the Start menu, and opens the web UI.

Same thing as one PowerShell line (replace OWNER):

```powershell
$env:DOCFLOW_REPO='OWNER/oneoffice-docauto'; $t=Read-Host 'GitHub token'; $env:DOCFLOW_GITHUB_TOKEN=$t; irm "https://api.github.com/repos/$env:DOCFLOW_REPO/contents/windows/get-docflow.ps1" -Headers @{Authorization="Bearer $t";Accept='application/vnd.github.raw'} | iex
```

## 4. Update

Any of these does the same:

- Web UI: **Settings > Updates & install > Update from GitHub**
- Start menu: **OneOffice DocFlow > Update OneOffice DocFlow**
- `windows\update.bat`

The updater:

1. checks the latest commit (or release) on GitHub and stops if you are already up to date;
2. stops DocFlow if it is running;
3. backs up the current code and database to `backups\` (keeps the last 5);
4. installs the new files, **keeping** `config\local.env` (settings and keys), `data\` (flows, documents, database), `tessdata\` and `.venv\`;
5. updates Python packages only when `requirements.txt` changed;
6. restarts DocFlow.

On the developer PC (a git working copy) it runs `git pull` instead of downloading.

To roll back, unzip the newest `backups\docflow-<date>.zip` over the folder.

## 5. Choose what PCs receive

`DOCFLOW_REF` in `config\local.env` decides what a PC installs:

| Value | PCs get | Use for |
|---|---|---|
| `main` (default) | Every change you push | Your own and test PCs |
| `release` | Only published releases | Customer PCs |
| a tag, e.g. `v3.1.0` | Exactly that version | Pinning a customer |

To publish a release: set the number in `VERSION` (e.g. `3.2.0`), commit, then in GitHub Desktop **Repository > Create tag** `v3.2.0` and push it, or create the release on github.com with that tag. The **Release** workflow runs the tests and attaches the zip and a ready-made installer.

## Troubleshooting

| Message | Fix |
|---|---|
| `GitHub refused access (HTTP 404/401)` | Wrong repository name, or the token has no access to it / has expired. Run the update again: it asks for a new token. |
| `git pull failed` (developer PC) | Commit or discard your local changes in GitHub Desktop, then update again. |
| `Port 8085 is used by ...` | Another program uses the port; change `DOCAUTO_PORT` in `config\local.env`. |
| `running scripts is disabled` | Use the `.bat` files; they bypass the policy for these scripts only. |
| GitHub Enterprise | Set `DOCFLOW_GITHUB_API=https://<your-host>/api/v3` before running the installer. |
