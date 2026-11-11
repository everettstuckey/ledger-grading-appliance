# Grade with the Microsoft Copilot desktop app

Ledger sends its transcription and grading jobs to a bridge on the Windows computer running Microsoft Copilot. The bridge automates the app's interface through local DevTools. It uses the app's Microsoft sign-in and needs no Microsoft API key. Microsoft Copilot handles the images and prompts under the work or school account signed in to the app. Use synthetic material for evaluation.

Ledger offers `copilot/gpt-5.6-sol`, `copilot/gpt-6.1-sol`, and `copilot/gpt-6-astra`. Select GPT-5.6 Sol explicitly when your Copilot account offers it. The bridge verifies the exact model and work account in the app before submitting a job. Model access varies by tenant and app. Check your desktop chat picker for the model you want to use.

A synthetic Windows batch verified GPT-5.6 Sol with the signed-in Copilot desktop app and produced the expected score, PDF, and CSV.

## Start Copilot with a dedicated profile

Close Copilot normally, including any instance still running in the notification area. Then open it from PowerShell with a dedicated Ledger Copilot profile. Reuse this profile on subsequent launches. Copilot 156.0.4314.58 requires a non-default profile for remote debugging.

~~~powershell
$copilot = ${env:ProgramFiles(x86)} + '\Microsoft\Copilot\Application\copilotapp.exe'
$profile = "$env:LOCALAPPDATA\Ledger\CopilotProfile"
$env:COPILOT_CDP_PORT = '9337'
& $copilot "--remote-debugging-port=$env:COPILOT_CDP_PORT" "--user-data-dir=$profile"
Get-NetTCPConnection -State Listen -LocalPort ([int]$env:COPILOT_CDP_PORT) | Select-Object LocalAddress, LocalPort
~~~

Confirm the debug port listens on `127.0.0.1` and only on loopback addresses. Sign in with your work or school account in Copilot and check the [enterprise data protection indicator](https://learn.microsoft.com/en-us/copilot/privacy-and-protections). The dedicated profile has its own sign-in state.

## Pair Ledger on the same Windows PC

Leave Copilot running. Before your first Ledger launch, open PowerShell in the repository and set the exact work or school email shown in Copilot.

~~~powershell
$env:LEDGER_COPILOT_LOCAL_ACCOUNT = 'teacher@example.org'
# Optional. Match the port you used to start Copilot. Default is 9337.
# $env:COPILOT_CDP_PORT = '9337'
# Optional. Select a model available to your account. Default is copilot/gpt-6.1-sol.
# $env:LEDGER_DEFAULT_MODEL = 'copilot/gpt-5.6-sol'
.\Start-Ledger.ps1
~~~

**System Initialization** selects local Copilot automatically. Review and accept the evaluation terms to finish setup. Ledger saves the work address and debugging port, then starts an authenticated bridge on loopback. It generates a fresh private bridge token each time it starts.

## Set up the Windows bridge manually

Use manual setup when Ledger and Copilot run on different machines or when maintaining an existing manual pairing. The bridge requires Python 3.9 or later on Windows.

1. [Start Copilot with its dedicated profile](#start-copilot-with-a-dedicated-profile) and sign in.
2. Generate a private bridge token with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
3. Set `COPILOT_EXPECTED_ACCOUNT` to the exact work email in Copilot, `COPILOT_BRIDGE_TOKEN` to the generated token, and `COPILOT_CDP_PORT` to the port used by Copilot.
4. From the repository, run `python bridge/copilot/copilot_bridge.py --probe` to verify the account and selected model. Then run `python bridge/copilot/copilot_bridge.py` to start the bridge on `127.0.0.1:8790`.
5. Enter the same token in Ledger's **System Initialization** screen. For an existing Debian installation, use the upgrade steps below.

From a separate Ledger host, including macOS or Debian, forward the bridge port through an existing approved SSH connection to the Windows computer. Keep the tunnel open while grading.

~~~bash
ssh -NT -o ExitOnForwardFailure=yes -L 127.0.0.1:8790:127.0.0.1:8790 operator@windows-host
~~~

## Run a synthetic batch

Select an available Copilot model in **AI Model**. Keep the work account selected and the Copilot chat available during the batch. The bridge accepts up to 20 images per request, including reference pages. For long essays, set `LEDGER_ESSAY_MAX_PAGES_PER_CALL` in the GUI environment to leave room for the reference pages. After a failed upload or send, check Copilot for a leftover draft before retrying.

## Existing Debian Ledger installations

Close Ledger and back up local modifications. The installer preserves the engine and daemon files on existing appliances, so copy the updated files after stopping the daemon.

~~~bash
sudo systemctl stop ledger-daemon.service
sudo install -o paragon -g ledger-teacher -m 0640 engine/ledger-main-minimus.py /opt/ledger/engine/
sudo install -o paragon -g ledger-teacher -m 0640 daemon/{vertex-api-daemon,azure_provider,codex_provider,copilot_provider,grading_schema}.py /opt/ledger/daemon/
sudo install -o paragon -g ledger-teacher -m 0640 .agents/skills/ledger-grading/SKILL.md /opt/ledger/daemon/ledger-grading.SKILL.md
sudoedit /etc/paragon/.env
~~~

Set `LEDGER_COPILOT_BRIDGE_URL="http://127.0.0.1:8790"` and `LEDGER_COPILOT_BRIDGE_TOKEN` to the same token used on Windows. The daemon waits up to 320 seconds by default. If you change `LEDGER_COPILOT_TIMEOUT_S`, keep it above the Windows `COPILOT_TIMEOUT_S` setting. Restart `ledger-daemon.service`, open Ledger, and select the Copilot model.
