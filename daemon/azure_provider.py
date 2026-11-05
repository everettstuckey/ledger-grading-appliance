"""Tenant-scoped Azure OpenAI Responses client for the Ledger daemon."""

import base64
import logging
import math
import re

import requests

from grading_schema import GRADING_INSTRUCTIONS, grading_fields, grading_json_schema, unwrap_grades


AZURE_DEFAULT_MODEL = "gpt-6.1-sol"
AZURE_MODELS = frozenset({AZURE_DEFAULT_MODEL, "gpt-6-astra"})
_TOKEN_SCOPE = "https://ai.azure.com/.default"
_ENDPOINT = re.compile(
    r"https://[a-z0-9][a-z0-9-]*\.(?:openai\.azure\.com|services\.ai\.azure\.com)/?",
    re.IGNORECASE | re.ASCII,
)
_TENANT_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.IGNORECASE)


class AzureOpenAIError(RuntimeError):
    """A provider error whose message is safe for the daemon log and GUI."""


class AzureOpenAIProvider:
    def __init__(self, endpoint, tenant_id, timeout_s, max_output_tokens):
        if not _ENDPOINT.fullmatch(endpoint):
            raise AzureOpenAIError(
                "invalid_request: LEDGER_AZURE_OPENAI_ENDPOINT must be an HTTPS Azure OpenAI "
                "or Foundry resource URL without a path."
            )
        if not _TENANT_ID.fullmatch(tenant_id):
            raise AzureOpenAIError(
                "invalid_request: LEDGER_AZURE_TENANT_ID must be the school's Entra tenant GUID."
            )
        try:
            self.timeout_s = float(timeout_s)
            self.max_output_tokens = int(max_output_tokens)
        except ValueError:
            raise AzureOpenAIError("invalid_request: Invalid Azure OpenAI timeout or output limit.") from None
        if not math.isfinite(self.timeout_s) or self.timeout_s <= 0 or not 16 <= self.max_output_tokens <= 128000:
            raise AzureOpenAIError("invalid_request: Invalid Azure OpenAI timeout or output limit.")

        self.responses_url = f"{endpoint.rstrip('/')}/openai/v1/responses"
        self.tenant_id = tenant_id.lower()

    def _get_access_token(self):
        try:
            from azure.identity import AzureCliCredential
        except ImportError:
            raise AzureOpenAIError(
                "invalid_request: Azure OpenAI authentication requires the azure-identity SDK."
            ) from None

        # The SDK's default log messages can include CLI diagnostics. Return only
        # the actionable error below and keep subprocess output out of the journal.
        logging.getLogger("azure.identity").setLevel(logging.CRITICAL)
        try:
            return AzureCliCredential(tenant_id=self.tenant_id).get_token(_TOKEN_SCOPE).token
        except Exception:
            raise AzureOpenAIError(
                "invalid_request: Azure OpenAI authentication failed. Check that Azure CLI is installed and the "
                "Ledger service account is signed in to the configured school tenant."
            ) from None

    def respond(self, model, prompt, pngs, *, instructions="", grading_schema=None):
        model = model.lower()
        if model not in AZURE_MODELS:
            raise AzureOpenAIError("invalid_request: Select gpt-6.1-sol or gpt-6-astra for Azure OpenAI.")
        content = [
            {
                "type": "input_image",
                "image_url": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
                "detail": "high",
            }
            for png in pngs
        ]
        content.append({"type": "input_text", "text": prompt})
        payload = {
            "model": model,
            "input": [{"role": "user", "content": content}],
            "store": False,
            "max_output_tokens": self.max_output_tokens,
        }
        if grading_schema is not None:
            fields = grading_fields(grading_schema)
            payload["text"] = {"format": {
                "type": "json_schema",
                "name": "ledger_grading_results",
                "strict": True,
                "schema": grading_json_schema(fields),
            }}
            instructions = f"{instructions}\n\n{GRADING_INSTRUCTIONS}".strip()
        if instructions:
            payload["instructions"] = instructions

        token = self._get_access_token()
        try:
            response = requests.post(
                self.responses_url,
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
                timeout=(10, self.timeout_s),
                allow_redirects=False,
            )
        except requests.RequestException:
            raise AzureOpenAIError("Azure OpenAI request failed before a response was received.") from None

        status = response.status_code
        if status == 401:
            raise AzureOpenAIError("Azure OpenAI HTTP 401. Verify the service account's school tenant sign-in.")
        if status == 403:
            raise AzureOpenAIError("Azure OpenAI HTTP 403. Verify the service account has inference access.")
        if status == 404:
            raise AzureOpenAIError(f"Azure OpenAI HTTP 404. Verify the {model} deployment on the Azure resource.")
        if not 200 <= status < 300:
            # Provider error bodies may echo the prompt or OCR content.
            raise AzureOpenAIError(f"Azure OpenAI request failed (HTTP {status}).")
        try:
            result = response.json()
        except ValueError:
            raise AzureOpenAIError("invalid_request: Azure OpenAI returned malformed response JSON.") from None
        text = _completed_text(result, model)
        if grading_schema is not None:
            return unwrap_grades(text, fields, "Azure OpenAI")
        return text


def _completed_text(result, model):
    if not isinstance(result, dict) or not isinstance(result.get("output"), list):
        raise AzureOpenAIError("invalid_request: Azure OpenAI returned a malformed response.")
    if result.get("status") != "completed" or result.get("incomplete_details") or result.get("error"):
        details = result.get("incomplete_details")
        if isinstance(details, dict) and details.get("reason") == "max_output_tokens":
            raise AzureOpenAIError(
                "invalid_request: Azure OpenAI reached its output limit. Raise "
                "LEDGER_AZURE_OPENAI_MAX_OUTPUT_TOKENS before retrying."
            )
        raise AzureOpenAIError("invalid_request: Azure OpenAI did not complete the response.")
    parts = []
    for item in result["output"]:
        if isinstance(item, dict) and item.get("type") == "reasoning":
            continue
        if (
            not isinstance(item, dict)
            or item.get("type") != "message"
            or item.get("role") != "assistant"
            or item.get("status", "completed") != "completed"
            or not isinstance(item.get("content"), list)
        ):
            raise AzureOpenAIError("invalid_request: Azure OpenAI returned a malformed response message.")
        for part in item["content"]:
            if isinstance(part, dict) and part.get("type") == "refusal":
                raise AzureOpenAIError("invalid_request: Azure OpenAI refused the request.")
            if not isinstance(part, dict) or part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                raise AzureOpenAIError("invalid_request: Azure OpenAI returned malformed response content.")
            parts.append(part["text"])

    text = "".join(parts)
    if not text.strip():
        raise AzureOpenAIError("invalid_request: Azure OpenAI returned no response text.")
    reported_model = result.get("model")
    if not isinstance(reported_model, str):
        raise AzureOpenAIError("invalid_request: Azure OpenAI did not identify the model that ran.")
    if not re.fullmatch(rf"{re.escape(model)}(?:-\d{{4}}-\d{{2}}-\d{{2}})?", reported_model):
        raise AzureOpenAIError("invalid_request: Azure OpenAI returned a different model than requested.")
    return text
