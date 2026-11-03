# OneOffice DocFlow on GitHub: install and update in one click

DocFlow lives in a **public GitHub repository**: https://github.com/OneOfficeautomation-Private-Limited/oneoffice-docauto. Every PC installs and updates from it, so nobody has to download zips or copy folders again, and no PC needs a GitHub account or token.

| Who | Does what | How often |
|---|---|---|
| You (developer PC) | Push changes with GitHub Desktop | Whenever the code changes |
| GitHub | Runs the tests and a full Windows install on a clean PC (CI) | Every push, automatically |
| Any PC | Double-clicks **Install-OneOffice-DocFlow.bat** | Once |
| Any PC | Clicks **Settings > Update from GitHub** (or Start menu > Update OneOffice DocFlow) | Whenever you publish a change |

Because the repository is public, anyone can read and download the code. Never commit keys or customer documents: the `.gitignore` already keeps `config\local.env` (settings and keys), `data\` (documents, flows, database), `tessdata\`, `backups\` and `.venv\` out of GitHub.

## 1. Developer PC: work on the repository

The repository is already published, with the automatic checks (CI) in `.github\workflows`.

1. Install **GitHub Desktop** (desktop.github.com) and sign in with an account that is a member of the OneOffice organisation.
2. **File > Add local repository** and choose `C:\Users\DELL\source\oneoffice-docauto` (or **File > Clone repository** on a new developer PC).
   Use *Add local repository*, not *New repository*: *New repository* creates an empty repository inside the folder instead of publishing the code.
3. Commit, then **Push origin**. On github.com open the repository > **Actions** to follow the CI run. Green = tests passed and DocFlow installed cleanly on a fresh Windows PC.

If the CI files are ever missing from `.github\workflows`, double-click **`windows\setup-github-ci.bat`** to copy them from `ci\workflows`.

## 2. Tokens: not needed

PCs download the public repository without signing in. GitHub allows about 60 anonymous requests an hour per network address, and each install or update uses three or four. That is roughly 20 installs or updates an hour from one office network, which is plenty for normal use.

If many PCs behind the same internet connection update at once, GitHub may refuse some of them for the rest of the hour (`HTTP 403`). The updater then asks for a token. Either wait an hour, or paste a token: any fine-grained token lifts the limit.

github.com > your picture > **Settings > Developer settings > Personal access tokens > Fine-grained tokens > Generate new token**

- Repository access: **Public repositories** (read-only)
- Permissions: none
- Expiration: up to 1 year

The token is asked once and stored encrypted for that Windows user. One token can be shared by your team's PCs.

A **private copy** of the repository (for example a customer fork) still works: the token then needs **Only select repositories** > that repository, with **Contents: Read-only**.

## 3. Install on a new PC

Get **Install-OneOffice-DocFlow.bat**, which already contains the repository name:

- in DocFlow: **Settings > Updates & install > Installer for another PC**, or
- from the repository's **Releases** page (attached to each release).

Double-click it and press **Enter** when it asks for a token. It installs Python and Tesseract if needed, downloads DocFlow to `%USERPROFILE%\OneOffice\DocFlow`, runs the tests, creates **OneOffice DocFlow** shortcuts on the Desktop and in the Start menu, and opens the web UI.

Same thing as one PowerShell line:

```powershell
$env:DOCFLOW_REPO='OneOfficeautomation-Private-Limited/oneoffice-docauto'; irm "https://api.github.com/repos/$env:DOCFLOW_REPO/contents/windows/get-docflow.ps1" -Headers @{Accept='application/vnd.github.raw'} | iex
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

Every push to `main` reaches all PCs on the `main` channel at their next update, so keep customer PCs on `release`.

To publish a release: set the number in `VERSION` (e.g. `3.2.0`), commit, then in GitHub Desktop **Repository > Create tag** `v3.2.0` and push it, or create the release on github.com with that tag. The **Release** workflow runs the tests and attaches the zip and a ready-made installer.

## Troubleshooting

| Message | Fix |
|---|---|
| `GitHub refused access (HTTP 403)` | GitHub's hourly limit for anonymous downloads was reached on this network. Wait an hour, or paste a token when asked (section 2). |
| `GitHub refused access (HTTP 404)` | Wrong repository name. It must be `OneOfficeautomation-Private-Limited/oneoffice-docauto` (or your private copy, which needs a token). |
| `GitHub refused access (HTTP 401)` | The saved token has expired or was deleted. Run the update again and paste a new token, or press Enter to continue without one. |
| `git pull failed` (developer PC) | Commit or discard your local changes in GitHub Desktop, then update again. |
| `Port 8085 is used by ...` | Another program uses the port; change `DOCAUTO_PORT` in `config\local.env`. |
| `running scripts is disabled` | Use the `.bat` files; they bypass the policy for these scripts only. |
| GitHub Enterprise | Set `DOCFLOW_GITHUB_API=https://<your-host>/api/v3` before running the installer. |
