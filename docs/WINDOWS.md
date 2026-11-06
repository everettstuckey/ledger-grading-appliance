# Run Ledger on Windows

Ledger runs as a native Windows desktop app with Python 3.12 x64. It starts its own local grading daemon under your Windows account and closes it when you exit. Setup, credentials, and the latest daemon log live under `%LOCALAPPDATA%\Ledger`. Your PDFs, class folders, and scores live in `Ledger Workspace` under your home folder.

## Install and open Ledger

1. Install [64-bit Python 3.12](https://www.python.org/downloads/windows/) with the Windows launcher.
2. Install [Tesseract OCR](https://tesseract-ocr.github.io/tessdoc/Installation.html#windows) with English recognition data. Ledger looks for it in PATH and common Windows installation folders.
3. Install [MiKTeX](https://miktex.org/howto/install-miktex) or [TeX Live](https://www.tug.org/texlive/windows.html) for PDF feedback. Ensure the XeLaTeX command and report packages are available to your Windows account. Windows reports use Times New Roman. XeLaTeX needs fontspec, geometry, graphics, eso-pic, mhchem, amsmath, amsfonts, xcolor, environ, and needspace.
4. Open PowerShell in this repository and run the launcher.

~~~powershell
.\Start-Ledger.ps1
~~~

The first run creates a user-level Python environment and installs the pinned dependencies. Run `.\Start-Ledger.ps1 -InstallOnly` to reinstall the dependencies without opening Ledger.

In **System Initialization**, configure an evaluation provider and review the evaluation terms. Ledger saves the settings in your Windows profile and starts the daemon. Use the [test batch instructions](INSTALL.md#part-d--before-the-first-test-run) for roster setup, calibration, grading, and export. Use synthetic documents for evaluation.

## Connect an AI provider

- **Copilot desktop.** Start the Windows bridge as described in the [Copilot setup guide](COPILOT_DESKTOP.md). If Ledger and Copilot run on the same PC, they connect over localhost. Enter the bridge token in Ledger and select the exact Copilot model in **AI Model**.
- **Codex desktop.** Enable Codex during setup. In **AI Model**, select the Codex model and use **Open Codex app**. The handoff directory lives in your Ledger workspace. See the [Codex setup guide](CODEX_DESKTOP.md).
- **Google, Anthropic, or Azure OpenAI.** Enter the provider's settings in the same wizard. For Azure, sign in with Azure CLI under the Windows account that runs Ledger.

## Local paths and troubleshooting

Set `LEDGER_WORKSPACE_PATH` to an absolute directory before launching if you want to keep the workspace elsewhere. Preserve the same directory on subsequent runs to keep your roster, scores, and scans together.

If Tesseract or XeLaTeX is installed in a different location, set `LEDGER_TESSERACT_PATH` or `LEDGER_XELATEX_PATH` to the full path of its executable before starting Ledger. If the daemon cannot start, check `%LOCALAPPDATA%\Ledger\daemon.log`. Treat configuration files and logs as private.

**Advanced → Factory reset** clears the current Windows user's provider settings. The workspace remains on disk.
