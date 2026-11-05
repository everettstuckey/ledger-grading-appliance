# Grade with the Codex desktop app

Ledger can hand transcription and grading jobs to Codex in the ChatGPT desktop app. Ledger writes each job to a local folder and reads back the result. Codex uses your ChatGPT sign-in. Ledger communicates with the app through files and needs no OpenAI API key.

Use blank or synthetic materials for evaluation. Codex processes the job images and rubric with OpenAI. Keep the handoff folder on the appliance or on storage your institution has approved. Use the normal educator review before relying on any score.

## Set up the handoff

1. In Ledger's **System Initialization**, select **Use Codex desktop for evaluation** and authorize the appliance. For an existing installation, see the upgrade instructions below.
2. In Ledger's **AI Model** window, save `codex/gpt-6.1-sol` or `codex/gpt-6-astra`, then reopen **AI Model** and choose **Open Codex app**. When a Codex link handler is installed, Ledger opens a draft chat in the handoff folder. Otherwise, Ledger displays the folder and prompt.
3. In the Codex desktop app, open that folder as a project. If the desktop app runs on a different computer, open the same folder through an approved file mount or use the app's [SSH connection](https://learn.chatgpt.com/docs/remote-connections#connect-to-an-ssh-host) as the account that owns the handoff folder. The SSH option requires setting up Codex on the remote host as documented by OpenAI. The `ledger-grading` skill is included in the folder.
4. In Codex, select GPT-6.1 Sol or GPT-6 Astra to match Ledger's model choice. Send `$ledger-grading Process Ledger jobs in this workspace while I run an evaluation batch.` The model picker in the desktop app controls which model handles the job.
5. Run a synthetic batch in Ledger and keep the Codex chat running. Codex reads the jobs as they appear. Ledger validates the files Codex returns before using the results. If the chat stopped after two quiet minutes, send the same prompt again. Ledger expires a pending job after five minutes by default.

If Codex is the only provider, Ledger defaults to `codex/gpt-6.1-sol` for accounts with no saved model or the original Gemini default. With multiple providers, choose the model explicitly in **AI Model**.

## Existing Ledger installations

The installer leaves existing engine and daemon files in place. To upgrade an earlier installation, close Ledger, back up any local source edits, and run the following commands from this checkout as the Ledger operator.

```bash
sudo bash build-ledger.sh --commit
sudo systemctl stop ledger-daemon.service
sudo install -o paragon -g ledger-teacher -m 0640 engine/ledger-main-minimus.py /opt/ledger/engine/
sudo install -o paragon -g ledger-teacher -m 0640 daemon/{vertex-api-daemon,azure_provider,codex_provider,grading_schema}.py /opt/ledger/daemon/
sudo install -o paragon -g ledger-teacher -m 0640 .agents/skills/ledger-grading/SKILL.md /opt/ledger/daemon/ledger-grading.SKILL.md
sudoedit /etc/paragon/.env
```

Add `LEDGER_CODEX_ENABLED="true"`. The default handoff folder is `.Codex Jobs` under the daemon user's `Ledger Workspace`. To choose another directory, set `LEDGER_CODEX_JOB_DIR` to its absolute path. The daemon account must own the folder and have read and write access. Restart `ledger-daemon.service`, open Ledger, and select the Codex model in **AI Model**.
