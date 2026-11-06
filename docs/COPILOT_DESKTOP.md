# Grade with the Microsoft Copilot desktop app

Ledger sends its transcription and grading jobs to a bridge on the Windows computer running Microsoft Copilot. The bridge automates the app's interface through local DevTools. It uses the app's Microsoft sign-in and needs no Microsoft API key. Microsoft Copilot handles the images and prompts under the work or school account signed in to the app. Use synthetic material for evaluation.

Ledger offers `copilot/gpt-5.6-sol`, `copilot/gpt-6.1-sol`, and `copilot/gpt-6-astra`. Select GPT-5.6 Sol explicitly when your Copilot account offers it. The bridge verifies the exact model and work account in the app before submitting a job. Model access varies by tenant and app. Check your desktop chat picker for the model you want to use.

A synthetic Windows batch verified GPT-5.6 Sol with the signed-in Copilot desktop app and produced the expected score, PDF, and CSV.

## Set up the Windows bridge

The bridge requires Python 3.9 or later on Windows.

1. Close the Copilot app normally, including any instance still running in the notification area. Open it from PowerShell with the debugging port and a dedicated Ledger Copilot profile, as shown below. Reuse that dedicated profile on subsequent launches.
2. Confirm that the chosen debugging port is listening on loopback. Copilot `156.0.4314.58` reports that remote debugging requires a non-default data directory when started with `Microsoft\Copilot\User Data`, even when the path is passed explicitly. Choosing another port still requires a dedicated profile.
3. Sign in to your Microsoft work or school account inside the dedicated Copilot profile. Check the [enterprise data protection indicator](https://learn.microsoft.com/en-us/copilot/privacy-and-protections) in the chat. The dedicated profile has its own sign-in state.
4. Generate a bridge token with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Enter the same token on Windows and in Ledger. Keep it in private configuration.
5. In the same PowerShell session, set `COPILOT_EXPECTED_ACCOUNT` to the work or school email shown by the Copilot app, and `COPILOT_BRIDGE_TOKEN` to the token. Run `python bridge/copilot/copilot_bridge.py` from the source checkout. The bridge listens on `127.0.0.1:8790`. Select an approved model in Copilot before running the bridge with `--probe`. The probe checks the desktop session without submitting a prompt.

~~~powershell
$copilot = ${env:ProgramFiles(x86)} + '\Microsoft\Copilot\Application\copilotapp.exe'
$profile = "$env:LOCALAPPDATA\Ledger\CopilotProfile"
$env:COPILOT_CDP_PORT = '9337'
& $copilot "--remote-debugging-port=$env:COPILOT_CDP_PORT" "--user-data-dir=$profile"
Get-NetTCPConnection -State Listen -LocalPort ([int]$env:COPILOT_CDP_PORT) | Select-Object LocalAddress, LocalPort
~~~

Use the result only if the port listens on loopback. Set `COPILOT_CDP_PORT` to the same value in any new PowerShell session that starts the bridge. The debugging port is separate from the bridge's HTTP port, `8790`. This is desktop UI automation and uses no supported Microsoft inference API.

## Connect the appliance

When Ledger and the bridge run on the same Windows PC, Ledger connects to the bridge over localhost. Follow the [Windows setup guide](WINDOWS.md) and enter the bridge token in **System Initialization**.

For a separate Debian appliance, forward the bridge port through an existing approved SSH connection to the Windows computer. Keep the tunnel open while grading.

~~~bash
ssh -NT -o ExitOnForwardFailure=yes -L 127.0.0.1:8790:127.0.0.1:8790 operator@windows-host
~~~

For a new installation, enter the token in Ledger's **System Initialization** screen. For an existing installation, use the upgrade steps below. Select an available Copilot model in **AI Model** and run a synthetic batch. Keep the work account selected and leave the Copilot chat available to the bridge during the batch. Ledger checks the returned data before accepting the transcription or grades. The bridge accepts up to 20 images per request, including reference pages. For long essays, set `LEDGER_ESSAY_MAX_PAGES_PER_CALL` in the GUI environment to leave room for the reference pages. After a failed upload or send, check the desktop app for a leftover draft before retrying.

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
