"""Exercise the Azure route through the daemon's public HTTP endpoints."""

import base64
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
from PIL import Image


DAEMON_PATH = Path(__file__).resolve().parents[1] / "daemon" / "vertex-api-daemon.py"
ENDPOINT = "https://ledger-test.openai.azure.com"
TENANT_ID = "00000000-0000-4000-8000-000000000001"
MODEL = "gpt-6.1-sol"
TOKEN = "synthetic-access-token"
SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "item_number": {"type": "STRING"},
            "student_answer": {"type": "STRING"},
            "status": {"type": "STRING"},
            "feedback": {"type": "STRING"},
            "points_earned": {"type": "NUMBER"},
        },
        "required": ["item_number", "student_answer", "status", "feedback", "points_earned"],
    },
}
GRADE = {
    "item_number": "1",
    "student_answer": "4",
    "status": "CORRECT",
    "feedback": "Correct sum.",
    "points_earned": 1,
}


def png(color):
    buffer = io.BytesIO()
    Image.new("RGB", (12, 8), color).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def upstream_response(body, status_code=200):
    response = requests.Response()
    response.status_code = status_code
    response.headers["Content-Type"] = "application/json"
    response._content = json.dumps(body).encode("utf-8")
    return response


def completed(value, model=MODEL):
    return upstream_response({
        "status": "completed",
        "model": model,
        "output": [{
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": json.dumps(value)}],
        }],
    })


class AzureDaemonTest(unittest.TestCase):
    def setUp(self):
        temp_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        env_path = temp_dir / "test.env"
        env_path.write_text(
            f"LEDGER_AZURE_OPENAI_ENDPOINT={ENDPOINT}\n"
            f"LEDGER_AZURE_TENANT_ID={TENANT_ID}\n",
            encoding="utf-8",
        )
        self.enterContext(patch.dict(os.environ, {"LEDGER_ENV_PATH": str(env_path)}, clear=True))
        self.enterContext(patch.object(sys, "path", [str(DAEMON_PATH.parent), *sys.path]))

        spec = importlib.util.spec_from_file_location("ledger_test_daemon", DAEMON_PATH)
        self.daemon = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.daemon)
        self.client = self.daemon.app.test_client()
        self.upstream = self.enterContext(patch("azure_provider.requests.post"))
        self.credential = self.enterContext(patch("azure.identity.AzureCliCredential"))
        self.credential.return_value.get_token.return_value.token = TOKEN
        self.master = png("blue")
        self.live = png("red")

    def stage_context(self):
        response = self.client.post("/api/cache/build", json={
            "system_instruction": "Grade against the blue master.",
            "images_b64": [self.master],
        })
        self.assertEqual(response.get_json(), {"status": "success", "text": "azure-openai-inline"})
        self.upstream.assert_not_called()

    def assert_request_images(self, *images):
        call = self.upstream.call_args
        self.assertEqual(call.args[0], f"{ENDPOINT}/openai/v1/responses")
        self.assertEqual(call.kwargs["headers"]["Authorization"], f"Bearer {TOKEN}")
        self.assertFalse(call.kwargs.get("allow_redirects", True))
        payload = call.kwargs["json"]
        content = [
            part for message in payload["input"] if isinstance(message.get("content"), list)
            for part in message["content"]
        ]
        self.assertEqual(
            [part["image_url"] for part in content if part["type"] == "input_image"],
            [f"data:image/png;base64,{image}" for image in images],
        )
        self.assertIs(payload["store"], False)
        return payload

    def test_azure_only_default_and_ocr(self):
        health = self.client.get("/").get_json()
        self.assertEqual(health["azure_openai_route"], "entra-cli")
        self.assertEqual(health["azure_openai_default_model"], MODEL)
        self.assertEqual(health["default_model"], MODEL)
        self.assertEqual(self.daemon._provider_for("gemini-4-argon"), "google")
        self.assertEqual(self.daemon._provider_for("claude-sonnet-5-5"), "anthropic")
        self.upstream.return_value = completed({"bartonian_id": 125})
        response = self.client.post("/api/silo1", json={
            "prompt": "Read the synthetic ID.",
            "image_b64": self.live,
        })
        result = response.get_json()
        self.assertEqual(result["status"], "success")
        self.assertEqual(json.loads(result["text"]), {"bartonian_id": 125})
        self.credential.assert_called_once_with(tenant_id=TENANT_ID)
        self.credential.return_value.get_token.assert_called_once_with("https://ai.azure.com/.default")
        payload = self.assert_request_images(self.live)
        self.assertEqual(payload["model"], MODEL)

    def test_explicit_astra_model_routes_to_azure(self):
        self.upstream.return_value = completed({"assignment_title": "Synthetic"}, "gpt-6-astra")
        response = self.client.post("/api/silo1", json={
            "model_name": "gpt-6-astra",
            "prompt": "Read the synthetic title.",
            "image_b64": self.live,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.assert_request_images(self.live)["model"], "gpt-6-astra")

    def test_boxed_grade_unwraps_strict_schema(self):
        self.stage_context()
        self.upstream.return_value = completed({"results": [GRADE]})
        response = self.client.post("/api/silo2", json={
            "prompt": "Grade the red response.",
            "image_b64": self.live,
            "schema": SCHEMA,
        })
        result = response.get_json()
        self.assertEqual(result["status"], "success")
        self.assertEqual(json.loads(result["text"]), [GRADE])
        payload = self.assert_request_images(self.master, self.live)
        format_spec = payload["text"]["format"]
        self.assertEqual(format_spec["type"], "json_schema")
        self.assertIs(format_spec["strict"], True)
        wrapper = format_spec["schema"]
        self.assertEqual(wrapper["type"], "object")
        self.assertEqual(wrapper["required"], ["results"])
        self.assertIs(wrapper["additionalProperties"], False)
        results = wrapper["properties"]["results"]
        self.assertEqual(results["type"], "array")
        self.assertEqual(results["items"]["properties"]["points_earned"]["type"], "number")
        self.assertCountEqual(results["items"]["required"], SCHEMA["items"]["required"])
        self.assertIs(results["items"]["additionalProperties"], False)

    def test_essay_master_override_is_scoped_to_one_call(self):
        self.stage_context()
        self.upstream.return_value = completed({"results": [GRADE]})
        override = png("green")
        second_page = png("yellow")
        response = self.client.post("/api/silo2", json={
            "prompt": "Grade both pages.",
            "images_b64": [self.live, second_page],
            "master_images_b64": [override],
            "schema": SCHEMA,
        })
        self.assertEqual(response.status_code, 200)
        self.assert_request_images(override, self.live, second_page)
        response = self.client.post("/api/silo2", json={"image_b64": self.live, "schema": SCHEMA})
        self.assertEqual(response.status_code, 200)
        self.assert_request_images(self.master, self.live)

    def test_delete_clears_context_without_provider_request(self):
        self.stage_context()
        self.assertTrue(self.client.get("/").get_json()["cache_active"])
        response = self.client.post("/api/cache/delete")
        self.assertEqual(response.get_json()["status"], "success")
        self.assertFalse(self.client.get("/").get_json()["cache_active"])
        with self.assertLogs("ledger-daemon", level="ERROR"):
            response = self.client.post("/api/silo2", json={"image_b64": self.live, "schema": SCHEMA})
        self.assertEqual(response.status_code, 500)
        self.assertIn("No active evaluation context", response.get_json()["message"])
        self.upstream.assert_not_called()

    def test_incomplete_refused_empty_or_wrong_model_cannot_return_grade(self):
        self.stage_context()
        cases = {
            "incomplete": upstream_response({
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": completed({"results": [GRADE]}).json()["output"],
            }),
            "refused": upstream_response({
                "status": "completed",
                "model": MODEL,
                "output": [{
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "refusal", "refusal": "Synthetic refusal detail."}],
                }],
            }),
            "empty": completed({"results": []}),
            "partial row": completed({"results": [{"points_earned": 1}]}),
            "invalid score": completed({"results": [{**GRADE, "points_earned": "1"}]}),
            "wrong model": completed({"results": [GRADE]}, "gpt-5"),
            "missing model": completed({"results": [GRADE]}, None),
        }
        for case, upstream in cases.items():
            with self.subTest(case=case):
                self.upstream.return_value = upstream
                with self.assertLogs("ledger-daemon", level="ERROR"):
                    response = self.client.post("/api/silo2", json={
                        "image_b64": self.live,
                        "schema": SCHEMA,
                    })
                self.assertEqual(response.status_code, 500)
                self.assertEqual(response.get_json()["status"], "error")
                self.assertNotIn("text", response.get_json())

    def test_rejects_endpoint_outside_azure_before_requesting_token(self):
        from azure_provider import AzureOpenAIError, AzureOpenAIProvider

        with self.assertRaisesRegex(AzureOpenAIError, "HTTPS Azure OpenAI"):
            AzureOpenAIProvider(
                f"{ENDPOINT}.example.org", TENANT_ID, timeout_s=30, max_output_tokens=1024
            )
        self.credential.assert_not_called()
        self.upstream.assert_not_called()

    def test_azure_cli_failure_does_not_expose_cli_diagnostics(self):
        sensitive = "synthetic CLI diagnostic"
        self.credential.return_value.get_token.side_effect = RuntimeError(sensitive)
        with self.assertLogs("ledger-daemon", level="ERROR") as logs:
            response = self.client.post("/api/silo1", json={"image_b64": self.live})
        self.assertEqual(response.status_code, 500)
        self.assertIn("authentication failed", response.get_json()["message"])
        self.assertNotIn(sensitive, response.get_data(as_text=True))
        self.assertNotIn(sensitive, "\n".join(logs.output))
        self.upstream.assert_not_called()

    def test_upstream_error_does_not_expose_response_content(self):
        sensitive = "synthetic upstream metadata and submitted text"
        self.upstream.return_value = upstream_response({
            "error": {"code": "BadRequest", "message": sensitive, "metadata": sensitive},
        }, status_code=400)
        with self.assertLogs("ledger-daemon", level="ERROR") as logs:
            response = self.client.post("/api/silo1", json={"image_b64": self.live})
        self.assertEqual(response.status_code, 500)
        self.assertIn("400", response.get_json()["message"])
        self.assertNotIn(sensitive, response.get_data(as_text=True))
        self.assertNotIn(sensitive, "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
