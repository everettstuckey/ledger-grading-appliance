# Grade with the Microsoft Copilot desktop app

Ledger sends its transcription and grading jobs to a bridge on the Windows computer running Microsoft Copilot. The bridge automates the app's interface through local DevTools. It uses the app's Microsoft sign-in and needs no Microsoft API key. Microsoft Copilot handles the images and prompts under the work or school account signed in to the app. Use synthetic material for evaluation.

Ledger offers `copilot/gpt-6.1-sol` and `copilot/gpt-6-astra`. The bridge checks the exact model and account in the app before submitting a job. Model access varies by tenant and app. Microsoft documents these model names for [Copilot Cowork](https://learn.microsoft.com/en-us/copilot/cowork/cowork-models). Verify that your work account exposes your chosen model in the desktop chat picker. The Ledger-to-bridge tests use synthetic images and a simulated desktop driver. A live image round trip on an eligible work account is still pending.

## Set up the Windows bridge

The bridge requires Python 3.9 or later on Windows.

1. Open the Microsoft Copilot desktop app and verify the work or school account. Check the [enterprise data protection indicator](https://learn.microsoft.com/en-us/copilot/privacy-and-protections) in the chat.
2. Close the Copilot app normally. Open it from PowerShell with the debugging port and the app's own profile path shown below. The bridge attaches to the app you start.
3. Confirm that port 9223 is listening on loopback. Some app versions may reject these flags. The bridge reports this as a setup error.
4. Generate a bridge token with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Enter the same token on Windows and in Ledger. Keep it in private configuration.
5. In a Windows PowerShell session, set `COPILOT_EXPECTED_ACCOUNT` to the work or school email shown by the Copilot app, and `COPILOT_BRIDGE_TOKEN` to the token. Run `python bridge/copilot/copilot_bridge.py` from the source checkout. The bridge listens on `127.0.0.1:8790`. Select an approved model in Copilot before running the bridge with `--probe`. The probe checks the desktop session without submitting a prompt.

~~~powershell
$copilot = ${env:ProgramFiles(x86)} + '\Microsoft\Copilot\Application\copilotapp.exe'
$profile = "$env:LOCALAPPDATA\Microsoft\Copilot\User Data"
& $copilot '--remote-debugging-port=9223' "--user-data-dir=$profile"
Get-NetTCPConnection -State Listen -LocalPort 9223 | Select-Object LocalAddress, LocalPort
~~~

Use the result only if the port listens on loopback. If you choose a different port, set `COPILOT_CDP_PORT` on Windows. This is desktop UI automation and uses no supported Microsoft inference API.

## Connect the appliance

When Ledger and the bridge run on the same Windows PC, Ledger connects to the bridge over localhost. Follow the [Windows setup guide](WINDOWS.md) and enter the bridge token in **System Initialization**.

For a separate Debian appliance, forward the bridge port through an existing approved SSH connection to the Windows computer. Keep the tunnel open while grading.

~~~bash
ssh -NT -o ExitOnForwardFailure=yes -L 127.0.0.1:8790:127.0.0.1:8790 operator@windows-host
~~~

For a new installation, enter the token in Ledger's **System Initialization** screen. For an existing installation, use the upgrade steps below. Select `copilot/gpt-6.1-sol` or `copilot/gpt-6-astra` in **AI Model** and run a synthetic batch. Keep the work account selected and leave the Copilot chat available to the bridge during the batch. Ledger checks the returned data before accepting the transcription or grades. The bridge accepts up to 20 images per request, including reference pages. For long essays, set `LEDGER_ESSAY_MAX_PAGES_PER_CALL` in the GUI environment to leave room for the reference pages. After a failed upload or send, check the desktop app for a leftover draft before retrying.

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
