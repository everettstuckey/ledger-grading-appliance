# Run Ledger on macOS

Ledger runs its desktop interface and grading daemon under your macOS account. The launcher creates a private Python environment and installs the pinned dependencies. Ledger closes its daemon when you quit.

## Install and open Ledger

1. Install [Python 3.12 with Tk](https://www.python.org/downloads/macos/). Set `LEDGER_PYTHON` to the full interpreter path to use a different Python 3.12 installation.
2. Install [Tesseract](https://tesseract-ocr.github.io/tessdoc/Installation.html) with English recognition data.
3. Install [MacTeX](https://tug.org/mactex/) for XeLaTeX reports. If using a minimal TeX Live distribution, install the report packages `fontspec`, `geometry`, `graphics`, `eso-pic`, `mhchem`, `amsmath`, `amsfonts`, `xcolor`, `environ`, and `needspace`. Reports use Times New Roman.
4. Open `Start-Ledger.command` in Finder or run it from a terminal in the checkout.

```bash
./Start-Ledger.command
```

Use `./Start-Ledger.command --install-only` to install or refresh Python dependencies without opening the interface. `LEDGER_TESSERACT_PATH` and `LEDGER_XELATEX_PATH` can point to executables in custom locations.

In **System Initialization**, choose an evaluation provider and review the evaluation terms. Follow the [test batch instructions](INSTALL.md#part-d--before-the-first-test-run) for the roster, calibration, grading, and export. Use synthetic material for evaluation.

## Evaluation providers

- **Codex desktop** uses Ledger's local job folder. Select the Codex model in Ledger and use **Open Codex app** to open the handoff. [Codex setup](CODEX_DESKTOP.md).
- **Google and Anthropic** use the provider credentials entered in the wizard. **Azure OpenAI** uses the Azure CLI sign-in under the account running Ledger. [Azure setup](AZURE_OPENAI.md).
- **Microsoft 365 Copilot** uses a Windows computer running the signed-in app and bridge. Set up the bridge on Windows, forward it to `127.0.0.1:8790` on the Mac using the guide's SSH command, and enter the bridge token in Ledger. Automatic pairing is available in Windows Ledger. [Copilot setup](COPILOT_DESKTOP.md#set-up-the-windows-bridge-manually).

## Local files

Ledger keeps the Python environment, setup files, and `daemon.log` in `~/Library/Application Support/Ledger`. Grading files live in `~/Ledger Workspace`. To use another workspace, set `LEDGER_WORKSPACE_PATH` to the absolute directory before launching and use the same path on subsequent runs. **Advanced → Factory reset** clears the provider setup and leaves the grading workspace on disk.
