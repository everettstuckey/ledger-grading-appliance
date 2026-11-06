"""Exercise Ledger's Copilot route against a mocked loopback bridge."""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

from test_azure_daemon import GRADE, SCHEMA, png, upstream_response


DAEMON_PATH = Path(__file__).resolve().parents[1] / "daemon" / "vertex-api-daemon.py"
BRIDGE_URL = "http://127.0.0.1:8790"
TOKEN = "synthetic_copilot_bridge_test_token"
MODEL = "copilot/gpt-6.1-sol"
SENSITIVE_TEXT = "synthetic sensitive bridge error detail"


def bridge_response(value, *, model=MODEL):
    response = upstream_response({"status": "success", "model": model, "text": json.dumps(value)})
    response._content_consumed = True
    return response


class CopilotDaemonTest(unittest.TestCase):
    def setUp(self):
        temp_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.env_path = temp_dir / "test.env"
        self.env_path.write_text(
            f"LEDGER_COPILOT_BRIDGE_URL={BRIDGE_URL}\n"
            f"LEDGER_COPILOT_BRIDGE_TOKEN={TOKEN}\n",
            encoding="utf-8",
        )
        self.enterContext(patch.dict(os.environ, {"LEDGER_ENV_PATH": str(self.env_path)}, clear=True))
        self.enterContext(patch.object(sys, "path", [str(DAEMON_PATH.parent), *sys.path]))
        self.session_factory = self.enterContext(patch("copilot_provider.requests.Session"))
        self.bridge = self.session_factory.return_value.__enter__.return_value

        self.daemon = self.load_daemon()
        self.client = self.daemon.app.test_client()
        self.master = png("blue")
        self.live = png("red")

    def load_daemon(self):
        spec = importlib.util.spec_from_file_location("ledger_copilot_test_daemon", DAEMON_PATH)
        daemon = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(daemon)
        return daemon

    def stage_context(self):
        response = self.client.post("/api/cache/build", json={
            "system_instruction": "Grade against the blue master.",
            "images_b64": [self.master],
        })
        self.assertEqual(response.get_json(), {"status": "success", "text": "copilot-inline"})
        self.session_factory.assert_not_called()

    def grade(self, **images):
        return self.client.post("/api/silo2", json={
            "prompt": "Grade the synthetic answer.",
            "schema": SCHEMA,
            **images,
        })

    def assert_request(self, model, *images):
        call = self.bridge.post.call_args
        self.assertEqual(call.args, (f"{BRIDGE_URL}/review",))
        self.assertEqual(call.kwargs["headers"], {"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(call.kwargs["timeout"], (5, 320))
        self.assertFalse(call.kwargs["allow_redirects"])
        self.assertFalse(self.bridge.trust_env)
        payload = call.kwargs["json"]
        self.assertEqual(payload.keys(), {"model", "prompt", "images_b64"})
        self.assertEqual(payload["model"], model)
        self.assertEqual(payload["images_b64"], list(images))
        return payload["prompt"]

    def test_passive_health_and_default_ocr_route(self):
        response = self.client.get("/")
        health = response.get_json()
        self.assertEqual(health["copilot_route"], "desktop-bridge")
        self.assertEqual(health["copilot_default_model"], MODEL)
        self.assertEqual(health["default_model"], MODEL)
        self.assertNotIn(TOKEN, response.get_data(as_text=True))
        self.session_factory.assert_not_called()
        self.assertEqual(self.daemon._provider_for("gpt-6.1-sol"), "azure-openai")
        self.assertEqual(self.daemon._provider_for("codex/gpt-6.1-sol"), "codex")

        self.bridge.post.return_value = bridge_response({"bartonian_id": 125})
        response = self.client.post("/api/silo1", json={
            "prompt": 'Read the synthetic ID as {"bartonian_id": 125}.',
            "image_b64": self.live,
        })
        self.assertEqual(response.get_json()["status"], "success")
        self.assertEqual(json.loads(response.get_json()["text"]), {"bartonian_id": 125})
        self.assertIn("Read the synthetic ID", self.assert_request(MODEL, self.live))

    def test_explicit_model_routes_and_wrong_ocr_field_is_rejected(self):
        model = "copilot/gpt-6-astra"
        payload = {
            "model_name": model,
            "prompt": 'Read the synthetic title as {"assignment_title": "Synthetic"}.',
            "image_b64": self.live,
        }
        self.bridge.post.return_value = bridge_response({"assignment_title": "Synthetic"}, model=model)
        response = self.client.post("/api/silo1", json=payload)
        self.assertEqual(json.loads(response.get_json()["text"]), {"assignment_title": "Synthetic"})
        self.assert_request(model, self.live)

        self.bridge.post.return_value = bridge_response({"bartonian_id": 125}, model=model)
        with self.assertLogs("ledger-daemon", level="ERROR"):
            response = self.client.post("/api/silo1", json=payload)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("text", response.get_json())

    def test_configured_sol_56_routes_cached_grading(self):
        model = "copilot/gpt-5.6-sol"
        with self.env_path.open("a", encoding="utf-8") as config:
            config.write(f"LEDGER_DEFAULT_MODEL={model}\n")
        self.client = self.load_daemon().app.test_client()
        health = self.client.get("/").get_json()
        self.assertEqual(health["default_model"], model)
        self.assertEqual(health["copilot_default_model"], model)

        self.stage_context()
        self.bridge.post.return_value = bridge_response({"results": [GRADE]}, model=model)
        response = self.grade(image_b64=self.live)
        self.assertEqual(response.get_json()["status"], "success")
        self.assertEqual(json.loads(response.get_json()["text"]), [GRADE])
        self.assert_request(model, self.master, self.live)

    def test_essay_override_and_boxed_grade_use_ordered_images(self):
        self.stage_context()
        self.bridge.post.return_value = bridge_response({"results": [GRADE]})
        replacement = png("green")
        second_reference = png("white")
        second_page = png("yellow")
        response = self.grade(
            images_b64=[self.live, second_page], master_images_b64=[replacement, second_reference]
        )
        self.assertEqual(response.get_json()["status"], "success")
        self.assertEqual(json.loads(response.get_json()["text"]), [GRADE])
        prompt = self.assert_request(MODEL, replacement, second_reference, self.live, second_page)
        self.assertIn("Reference page count is 2", prompt)
        self.assertIn("Grade against the blue master.", prompt)
        self.assertIn("Grade the synthetic answer.", prompt)
        self.assertIn('"required": ["results"]', prompt)

        response = self.grade(image_b64=self.live)
        self.assertEqual(response.get_json()["status"], "success")
        prompt = self.assert_request(MODEL, self.master, self.live)
        self.assertIn("Reference page count is 1", prompt)

    def test_failed_or_unverified_results_cannot_return_a_grade(self):
        self.stage_context()
        failures = {
            "wrong model": bridge_response({"results": [GRADE]}, model="copilot/gpt-6-astra"),
            "missing model": bridge_response({"results": [GRADE]}, model=None),
            "partial row": bridge_response({"results": [{"feedback": SENSITIVE_TEXT}]}),
            "bad score": bridge_response({"results": [{**GRADE, "points_earned": "1"}]}),
            "refusal": bridge_response(SENSITIVE_TEXT),
            "bridge error": upstream_response({"status": "error", "message": SENSITIVE_TEXT}),
            "redirect": upstream_response({"message": SENSITIVE_TEXT}, status_code=302),
        }
        for case, upstream in failures.items():
            with self.subTest(case=case):
                upstream._content_consumed = True
                self.bridge.post.return_value = upstream
                with self.assertLogs("ledger-daemon", level="ERROR") as logs:
                    response = self.grade(image_b64=self.live)
                self.assertEqual(response.status_code, 500)
                self.assertNotIn("text", response.get_json())
                self.assertNotIn(SENSITIVE_TEXT, response.get_data(as_text=True))
                self.assertNotIn(SENSITIVE_TEXT, "\n".join(logs.output))

    def test_timeout_is_sanitized_and_unsupported_model_never_reaches_bridge(self):
        self.bridge.post.side_effect = requests.Timeout(SENSITIVE_TEXT)
        with self.assertLogs("ledger-daemon", level="ERROR") as logs:
            response = self.client.post("/api/silo1", json={
                "prompt": "Read the synthetic ID.",
                "image_b64": self.live,
            })
        self.assertEqual(response.status_code, 500)
        self.assertIn("timed out", response.get_json()["message"])
        self.assertNotIn(SENSITIVE_TEXT, response.get_data(as_text=True))
        self.assertNotIn(SENSITIVE_TEXT, "\n".join(logs.output))

        self.bridge.post.reset_mock()
        with self.assertLogs("ledger-daemon", level="ERROR"):
            response = self.client.post("/api/silo1", json={
                "model_name": "copilot/gpt-5",
                "prompt": "Read the synthetic ID.",
                "image_b64": self.live,
            })
        self.assertEqual(response.status_code, 500)
        self.bridge.post.assert_not_called()

    def test_delete_clears_cached_context_without_bridge_call(self):
        self.stage_context()
        self.assertTrue(self.client.get("/").get_json()["cache_active"])
        self.assertEqual(self.client.post("/api/cache/delete").get_json()["status"], "success")
        self.assertFalse(self.client.get("/").get_json()["cache_active"])
        with self.assertLogs("ledger-daemon", level="ERROR"):
            response = self.grade(image_b64=self.live)
        self.assertIn("No active evaluation context", response.get_json()["message"])
        self.session_factory.assert_not_called()

    def test_gemini_and_claude_keep_their_normal_routes_with_copilot_enabled(self):
        with self.env_path.open("a", encoding="utf-8") as config:
            config.write("LEDGER_GOOGLE_API_KEY=synthetic-google-test\n")
            config.write("LEDGER_ANTHROPIC_API_KEY=synthetic-anthropic-test\n")
        with patch("google.genai.Client") as google_factory, patch("anthropic.Anthropic") as claude_factory:
            client = self.load_daemon().app.test_client()
            google = google_factory.return_value
            claude = claude_factory.return_value
            health = client.get("/").get_json()
            self.assertEqual(health["google_route"], "developer-api")
            self.assertEqual(health["anthropic_route"], "direct-api")
            self.assertEqual(health["copilot_route"], "desktop-bridge")
            self.assertNotIn("copilot_default_model", health)
            self.assertEqual(health["default_model"], "gemini-4-argon")

            ocr = json.dumps({"bartonian_id": 125})
            grade = json.dumps([GRADE])
            google.models.generate_content.side_effect = [
                SimpleNamespace(text=ocr),
                SimpleNamespace(text=grade),
            ]
            google.caches.create.return_value.name = "cachedContents/synthetic"
            claude.messages.create.side_effect = [
                SimpleNamespace(content=[{"type": "text", "text": ocr}]),
                SimpleNamespace(content=[{"type": "text", "text": grade}], stop_reason="end_turn"),
            ]

            for model, cache_name in (
                ("gemini-4-argon", "cachedContents/synthetic"),
                ("claude-sonnet-5-5", "anthropic-local"),
            ):
                with self.subTest(model=model):
                    response = client.post("/api/silo1", json={
                        "model_name": model, "prompt": "Read the ID.", "image_b64": self.live,
                    })
                    self.assertEqual(response.get_json(), {"status": "success", "text": ocr})
                    response = client.post("/api/cache/build", json={
                        "model_name": model,
                        "system_instruction": "Use this master.",
                        "images_b64": [self.master],
                    })
                    self.assertEqual(response.get_json(), {"status": "success", "text": cache_name})
                    response = client.post("/api/silo2", json={
                        "prompt": "Grade this answer.", "image_b64": self.live, "schema": SCHEMA,
                    })
                    self.assertEqual(response.get_json(), {"status": "success", "text": grade})
                    self.assertEqual(client.post("/api/cache/delete").get_json()["status"], "success")

            google.caches.delete.assert_called_once_with(name="cachedContents/synthetic")
            self.assertEqual(google.models.generate_content.call_count, 2)
            self.assertEqual(claude.messages.create.call_count, 2)
        self.session_factory.assert_not_called()

    def test_copilot_is_disabled_without_explicit_bridge_url(self):
        self.env_path.write_text("LEDGER_GOOGLE_API_KEY=synthetic-google-test\n", encoding="utf-8")
        with patch("google.genai.Client"):
            client = self.load_daemon().app.test_client()
        self.assertEqual(client.get("/").get_json()["copilot_route"], "absent")
        with self.assertLogs("ledger-daemon", level="ERROR"):
            response = client.post("/api/silo1", json={
                "model_name": MODEL,
                "prompt": "Read the ID.",
                "image_b64": self.live,
            })
        self.assertEqual(response.status_code, 500)
        self.assertIn("LEDGER_COPILOT_BRIDGE_URL", response.get_json()["message"])
        self.session_factory.assert_not_called()

    def test_provider_rejects_unsafe_url_token_or_timeout_without_connecting(self):
        from copilot_provider import CopilotProvider, CopilotProviderError

        for url, token, timeout in (
            ("http://localhost:8790", TOKEN, 320),
            (f"{BRIDGE_URL}/review", TOKEN, 320),
            ("https://127.0.0.1:8790", TOKEN, 320),
            ("http://127.0.0.1:65536", TOKEN, 320),
            (BRIDGE_URL, "short", 320),
            (BRIDGE_URL, f"{TOKEN} ", 320),
            (BRIDGE_URL, TOKEN, 321),
            (BRIDGE_URL, TOKEN, 4),
            (BRIDGE_URL, TOKEN, "nan"),
        ):
            with self.subTest(url=url, timeout=timeout):
                with self.assertRaises(CopilotProviderError):
                    CopilotProvider(url, token, timeout)
        self.session_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
