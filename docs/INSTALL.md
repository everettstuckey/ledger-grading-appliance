# Building an Evaluation Machine

> Experimental software for evaluation by school and district IT professionals; see [DISCLAIMER.md](../DISCLAIMER.md).
> These steps build a **test** machine with no disk encryption, hardware-key login or kiosk lockdown. Use test material, and get the approvals your institution requires before processing any student work.

This procedure is written for a technically qualified operator setting up a dedicated test machine. It was exercised from a blank disk on 2026-10-24 (Lenovo ThinkCentre M700 Tiny, Debian 13.7). Every command is typed into a terminal, then Enter.

## Requirements

- A computer **dedicated to testing**, holding nothing else of value, with **Debian 13** installed. The build script checks the version and refuses anything else.
- Choices that matter when you install Debian:
  - **Xfce** desktop.
  - **No disk encryption, no LVM.** This is the configuration the build was tested in. It means anyone with the machine or its drive can read everything on it.
  - Username **`paragon`**, exactly. (The program checks for this name.)
  - Leave the root password empty so the `paragon` account gets administrator rights.
- An internet connection your institution permits for this purpose.
- **A test key for one AI provider** — Google or Anthropic — on an account your institution controls. You can enter both.

## Part A — Get the files

1. Log in as `paragon`. Open a terminal: **Applications menu → Terminal Emulator**.
2. Install git. Type your password when asked; nothing shows while you type, which is normal.
   ```
   sudo apt install -y git
   ```
3. Download this repository into your home folder:
   ```
   git clone https://github.com/MOChemistry/ledger-grading-appliance.git
   ```
   Success: a folder named `ledger-grading-appliance` appears in your home folder.
4. Move into it:
   ```
   cd ~/ledger-grading-appliance
   ```

## Part B — Build

5. Optional preview. This shows what the script will do and changes nothing:
   ```
   sudo bash build-ledger.sh
   ```
   The last line should say `DONE   mode: DRY-RUN`. If a line starts with `REFUSING`, read it — it says what is wrong.
6. The real build. Stay logged in to the desktop while it runs:
   ```
   sudo bash build-ledger.sh --commit
   ```
   It downloads a lot and takes a while. It ends with `DONE   mode: COMMIT`. A log is saved next to the script. Running it again is safe; finished steps are skipped.

   Do not run `sudo apt autoremove` on this machine.

## Part C — First-time setup

7. Set up an AI provider for evaluation.
   - **Google AI Studio:** choose **Get API key**. The setup screen will require you to confirm that billing is enabled on the key's project. Google's terms differ for unpaid and paid use; read them and have your institution review them. Keys start with `AQ.` or `AIza`.
   - **Anthropic:** create a key in the Anthropic Console. It starts with `sk-ant-`. Read Anthropic's terms as well.
   - **Azure OpenAI** uses a model deployment and a Microsoft Entra sign-in. Follow the [Azure setup instructions](AZURE_OPENAI.md) before opening Ledger.
   - **Codex desktop** uses your existing ChatGPT sign-in. Follow the [desktop handoff instructions](CODEX_DESKTOP.md).
8. Double-click **Start Ledger** on the desktop. The first time, Xfce asks whether to trust the launcher: click **Mark Executable**. It asks once.
9. On the **SYSTEM INITIALIZATION** screen:
   1. Enter the provider settings. For Azure OpenAI, enter the resource URL and tenant ID.
   2. If you entered a Google key, the **Billing and Student Work — Required Reading** screen opens. Read it, tick the confirmation, click **CONTINUE**.
   3. Read the **evaluation terms**. Type the operator's name and tick the agreement box. If you do not agree to every term, stop here.
   4. Click **AUTHORIZE APPLIANCE** and type your password.
10. The diagnostic log (the big dark box) ends with a line like `AUTHORIZATION EXECUTED … Providers: GOOGLE (AI Studio)` or `… Providers: ANTHROPIC`. The status in the sidebar reads **SYSTEM: IDLE**.
11. Click **AI MODEL** and type the exact model to use, for example `gemini-4-argon`, `claude-fable-5`, or `gpt-6.1-sol`. Click **SAVE MY MODEL**.

## Part D — Before the first test run

12. **Roster.** Click **MANAGE ROSTER** and add test entries: a pseudonym ID, first name, last name. Rules:
    - The ID is a **three-digit number**, and its **first digit is a group number** (1–7). ID 312 is filed under `3rd Period`.
    - Never use real names or district-issued student IDs during evaluation.
    - A scanned page whose ID isn't on the roster is discarded with "Invalid/missing ID".
13. **Scoring instructions.** None ship with the software. Click **EDIT LEDGER PROMPT**, type or paste instructions, click **SAVE PROMPT**. They persist across restarts and a factory reset. Until they're saved, **RUN** refuses with a red **NO GRADING INSTRUCTIONS**.
    - Examples are in the `prompts/` folder (`~/ledger-grading-appliance/prompts`). They are examples for evaluation, not validated rubrics.
14. **Master and max score.** Put the blank copy of the test document (PDF) in `Ledger Workspace/01 Master Assignments`, click **LOAD MASTER PDF** and pick it. Type its total points in the **MAX SCORE** box. RUN refuses without both.
15. **Mode.** The switch at the top: **WORKSHEET (BOXED)** for forms with answer boxes, **ESSAY (FULL-PAGE)** for free writing. Worksheet is the default. **ESSAY mode sends whole pages to the provider** — see "What leaves the machine" in the [README](../README.md).
16. **Calibrate the scanner** once. Put one scanned test page in `02 Inbox Scans`, click **CALIBRATE SCANNER**, drag the three boxes onto the title, the ID and the page number, click **SAVE**.

## Part E — Running a test batch

17. Scan the test documents into one or more PDFs and drop them in `Ledger Workspace/02 Inbox Scans`.
18. Click **RUN**. The calibration window opens so you can check the three boxes; click **SAVE & PROCEED**. Closing it with the **X** cancels the run. Once the boxes are saved since starting the program, later runs skip this check; restarting the program brings it back.
19. When it finishes, the log shows `=== BATCH PROCESSING CONCLUDED. TEMP FOLDER SCRUBBED. ===`. Results are in `03 Classes/<group>/Evaluated Assignments/<assignment>/`: one conspectus PDF per ID and a `Tabula.csv` score list. The original scans move to `05 Archive Originals`. **OPEN GRADEBOOK** shows all scores and can export a CSV to `04 SIS Exports`.
    - **Review every score and comment.** AI output can be wrong.
    - If the daemon log shows `cached=None`, the provider's caching threshold was not met. Scoring is unaffected; input costs are higher.

## Part F — Other accounts

20. Other users on this build are ordinary Linux accounts, created with `adduser`. Only `paragon` (or another administrator account) can run first-time setup and use the **ADVANCED** menu.

## Part G — Factory reset and data

21. **ADVANCED → FACTORY RESET APPLIANCE**, then your password. This deletes the saved key(s) and stops the AI service. **It does not delete** the roster, scores, scans, conspectus PDFs, calibration or scoring instructions; they remain unencrypted on disk. To end an evaluation, wipe the drive.
