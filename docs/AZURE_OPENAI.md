# Azure OpenAI with Microsoft Entra ID

Ledger can use `gpt-6.1-sol` for both image transcription and grading. `gpt-6-astra` is also available when deployed. This integration uses Azure OpenAI Responses with a Microsoft Entra sign-in. Microsoft 365 Copilot Chat uses a separate service and license.

Use a resource your institution controls and evaluate with blank or synthetic material. Requests are billed to the Azure subscription. Azure receives the images described in [What leaves the machine](../README.md#what-leaves-the-machine) and the grading instructions. Review the deployment's processing region and [Azure data handling](https://learn.microsoft.com/azure/ai-foundry/responsible-ai/openai/data-privacy) with your institution before using student work.

## Set up the resource and sign in

1. Deploy `gpt-6.1-sol` in an Azure OpenAI resource. Name the deployment `gpt-6.1-sol`. To offer GPT-6 Astra, deploy it under `gpt-6-astra`.
2. At the resource scope, give the institution account that will run Ledger the **Cognitive Services OpenAI User** role. Record the resource URL and the resource tenant ID. [Azure OpenAI roles](https://learn.microsoft.com/azure/ai-foundry/openai/how-to/role-based-access-control)
3. Install [Azure CLI for Debian](https://learn.microsoft.com/cli/azure/install-azure-cli-linux) on the Ledger machine. In a terminal as `paragon`, sign in to the resource tenant and select the correct institutional account.

   ```bash
   az login --tenant '<tenant-id>' --allow-no-subscriptions --use-device-code --output none
   ```

   The Ledger service runs as `paragon` and uses that account's Azure CLI sign-in. If the tenant requires reauthentication, run the same command as `paragon`. Azure CLI keeps its sign-in separate from the Ledger settings.

## Configure Ledger

In **System Initialization**, enter the Azure OpenAI resource URL (for example, `https://example.openai.azure.com`) and the Microsoft Entra tenant ID. Complete the evaluation agreement and authorize Ledger. When Azure is the only provider, Ledger uses `gpt-6.1-sol` if the account has no saved model or still has the original Gemini default. With multiple providers or another saved model, choose `gpt-6.1-sol` in **AI Model**. The displayed model ID must match the Azure deployment name.

### Existing Ledger installations

The installer preserves installed engine and daemon files on rerun. Close Ledger, run `sudo bash build-ledger.sh --commit` from this checkout to install the dependencies, then update the three files below. Back up local source changes before copying.

```bash
sudo systemctl stop ledger-daemon.service
sudo install -o paragon -g ledger-teacher -m 0640 engine/ledger-main-minimus.py /opt/ledger/engine/
sudo install -o paragon -g ledger-teacher -m 0640 daemon/vertex-api-daemon.py daemon/azure_provider.py /opt/ledger/daemon/
sudoedit /etc/paragon/.env
```

Add these lines with your resource details.

```dotenv
LEDGER_AZURE_OPENAI_ENDPOINT="https://example.openai.azure.com"
LEDGER_AZURE_TENANT_ID="<tenant-id>"
```

Run `sudo systemctl start ledger-daemon.service`. Open Ledger and select `gpt-6.1-sol` in **AI Model**. The loopback health endpoint at `http://127.0.0.1:5000/` can confirm the route is configured. Run a synthetic assignment to verify sign-in, the model deployment, and scoring.
