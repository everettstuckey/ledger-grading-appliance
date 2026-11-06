import base64
import http.client
import json
import shutil
import subprocess
import sys
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge" / "copilot"))
import copilot_bridge as bridge

MODEL = "copilot/gpt-6.1-sol"
TOKEN = "synthetic-test-token_" * 2
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZEK8AAAAASUVORK5CYII="
PAYLOAD = {"prompt": "Score this synthetic page.", "images_b64": [PNG], "model": MODEL}


class ValidationTests(unittest.TestCase):
    def test_only_exact_work_chat_and_loopback_debugger_are_allowed(self):
        valid = {
            "type": "webview",
            "url": "https://cr.m365copilotapp.svc.cloud.microsoft/hwav2/chat",
            "id": "FAKE",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9223/devtools/page/FAKE",
        }
        self.assertEqual(bridge._debugger_path(valid, 9223), "/devtools/page/FAKE")
        for key, value in [
            ("url", "https://copilot.microsoft.com/hwav2/chat"),
            ("url", "https://cr.m365copilotapp.svc.cloud.microsoft.example.org/hwav2/chat"),
            ("url", "https://cr.m365copilotapp.svc.cloud.microsoft/hwav2/chat-other"),
            ("url", "https://cr.m365copilotapp.svc.cloud.microsoft/hwav2/chat/../other"),
            ("webSocketDebuggerUrl", "ws://example.org:9223/devtools/page/FAKE"),
            ("webSocketDebuggerUrl", "ws://localhost:9224/devtools/page/FAKE"),
            ("webSocketDebuggerUrl", "ws://127.0.0.1:9223/devtools/page/OTHER"),
        ]:
            with self.subTest(key=key, value=value):
                self.assertIsNone(bridge._debugger_path(dict(valid, **{key: value}), 9223))

    def test_requires_exact_known_model_label(self):
        self.assertEqual(bridge._label_id("GPT-6.1 Sol Think deeper"), MODEL)
        self.assertEqual(bridge._label_id("GPT 6 Astra"), "copilot/gpt-6-astra")
        for label in ("GPT-5.6 Sol Think deeper", "GPT 6.1 Sol preview", "Not GPT 6.1 Sol", "Auto"):
            with self.subTest(label=label):
                self.assertIsNone(bridge._label_id(label))

    def test_validates_exact_contract_and_image(self):
        prompt, pages, model = bridge._parse_review(json.dumps(PAYLOAD).encode())
        self.assertEqual((prompt, model), (PAYLOAD["prompt"], MODEL))
        self.assertEqual(pages, [(".png", base64.b64decode(PNG))])
        for changes in (
            {"allowed_models": ["Auto"]},
            {"model": "copilot/gpt-5.6-sol"},
            {"prompt": ["unexpected"]},
            {"images_b64": ["data:image/png;base64," + PNG]},
            {"images_b64": [base64.b64encode(b"not a page").decode()]},
            {"images_b64": [base64.b64encode(base64.b64decode(PNG)[:-12]).decode()]},
            {"images_b64": [PNG] * 21},
        ):
            with self.subTest(changes=changes.keys()), self.assertRaises(bridge.BridgeError):
                bridge._parse_review(json.dumps(dict(PAYLOAD, **changes)).encode())

    def test_settings_require_bearer_account_and_timeout_floor(self):
        env = {"COPILOT_BRIDGE_TOKEN": TOKEN, "COPILOT_EXPECTED_ACCOUNT": "Teacher@example.org"}
        settings = bridge.Settings.from_env(env)
        self.assertEqual(settings.account, "teacher@example.org")
        self.assertEqual(settings.timeout, 300)
        self.assertNotIn(TOKEN, repr(settings))
        for change in (
            {"COPILOT_BRIDGE_TOKEN": "too short"},
            {"COPILOT_BRIDGE_TOKEN": " " + TOKEN},
            {"COPILOT_EXPECTED_ACCOUNT": ""},
            {"COPILOT_TIMEOUT_S": "301"},
        ):
            with self.subTest(change=change.keys()), self.assertRaises(bridge.BridgeError):
                bridge.Settings.from_env(dict(env, **change))


class ChatGateTests(unittest.TestCase):
    def setUp(self):
        settings = bridge.Settings(TOKEN, "teacher@example.org")
        self.driver = bridge.ChatDriver(settings, time.monotonic() + 30)
        self.driver.session = Mock()
        self.driver._assert_account = Mock()
        self.driver._select_model = Mock()
        self.driver._pause = Mock()

    def test_missing_new_chat_never_uploads(self):
        self.driver._js = Mock(return_value=False)
        with self.assertRaisesRegex(bridge.BridgeError, "New chat"):
            self.driver.review(PAYLOAD["prompt"], ["test.png"], MODEL)
        self.driver.session.call.assert_not_called()
        self.driver._select_model.assert_not_called()

    def test_model_below_floor_never_uploads(self):
        self.driver._js = Mock(side_effect=[True, True, "GPT-5.6 Sol Think deeper"])
        with self.assertRaisesRegex(bridge.BridgeError, "before upload"):
            self.driver.review(PAYLOAD["prompt"], ["test.png"], MODEL)
        self.driver.session.call.assert_not_called()

    def test_lost_model_after_upload_never_types_or_submits(self):
        self.driver._js = Mock(side_effect=[True, True, "GPT 6.1 Sol", "input-object", 1, "Auto"])
        with self.assertRaisesRegex(bridge.BridgeError, "before submission"):
            self.driver.review(PAYLOAD["prompt"], ["test.png"], MODEL)
        self.driver.session.call.assert_called_once_with(
            "DOM.setFileInputFiles", {"files": ["test.png"], "objectId": "input-object"})
        self.assertEqual(self.driver._js.call_count, 6)



class ResponseExtractionTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the synthetic DOM fixture")
    def test_only_extracts_completed_response_from_current_turn(self):
        fixture = r"""
const assert = require('node:assert/strict');

function element({text = '', attributes = {}, children = [], button = false, content = false} = {}) {
  const node = {
    textContent: text,
    innerText: text,
    isConnected: true,
    getAttribute: name => attributes[name] ?? null,
    getClientRects: () => [{}],
    matches: () => false,
    contains: other => node === other || children.some(child => child.contains(other)),
    querySelectorAll: selector => children.flatMap(child => {
      const selected = (selector.startsWith('[data-message-author-role]') &&
        child.getAttribute('data-message-author-role')) ||
        (selector === 'button, [role="button"]' && child.isButton) ||
        (selector.includes('message-content') && child.isContent);
      return [...(selected ? [child] : []), ...child.querySelectorAll(selector)];
    }),
    querySelector: selector => node.querySelectorAll(selector)[0] || null,
    isButton: button,
    isContent: content,
  };
  for (const child of children) child.parentElement = node;
  return node;
}

const control = name => element({attributes: {'aria-label': name}, button: true});
const turn = (role, text, controls = []) => element({
  text,
  attributes: {'data-message-author-role': role},
  children: [element({text, content: true}), ...controls],
});
const getComputedStyle = () => ({visibility: 'visible'});
const prompt = 'Score this synthetic page.\n\nApply the rubric.';
const result = 'The feedback includes the words Copilot said as an example.';
const user = turn('user', prompt);
user.querySelector('[data-testid="user-message-content"]').textContent = prompt.replace(/\n/g, '');
const completed = turn('assistant', result, [control('Copy response'), control('Try again')]);
let document;

function setTranscript(children, otherControls = []) {
  const body = element({children: [element({children}), ...otherControls]});
  document = Object.assign(element({children: [body]}), {body});
}
setTranscript([user, completed]);
assert.equal(lastReply(prompt), result);
assert.equal(lastReply('A different prompt'), null);
assert.equal(lastReply('synthetic page.'), null);

setTranscript([user, turn('assistant', result, [control('Copy response')])], [control('Try again')]);
assert.equal(lastReply(prompt), null);
setTranscript([user, completed], [control('Stop generating')]);
assert.equal(lastReply(prompt), null);
setTranscript([turn('user', 'Old prompt'), completed, user, completed]);
assert.equal(lastReply(prompt), null);
"""
        subprocess.run(
            [shutil.which("node"), "-"], input=bridge.JS_HELPERS + fixture,
            text=True, capture_output=True, check=True, timeout=10,
        )


class HTTPTests(unittest.TestCase):
    def test_loopback_auth_size_and_valid_response_with_offline_driver(self):
        driver = MagicMock()
        driver.__enter__.return_value = driver
        staged = []
        def fake_review(prompt, image_files, model):
            self.assertEqual((prompt, model), (PAYLOAD["prompt"], MODEL))
            staged.extend(map(Path, image_files))
            self.assertTrue(staged[0].is_file())
            return "Synthetic feedback"
        driver.review.side_effect = fake_review
        driver.health.return_value = {"status": "ok", "model": MODEL}
        settings = bridge.Settings(TOKEN, "teacher@example.org", port=0)
        with patch.object(bridge, "ChatDriver", return_value=driver):
            with bridge.BridgeServer(settings) as server:
                worker = threading.Thread(target=server.serve_forever)
                worker.start()
                try:
                    def request(method, path, body=None, headers=None):
                        with closing(http.client.HTTPConnection(
                            "127.0.0.1", server.server_address[1], timeout=3,
                        )) as connection:
                            connection.request(method, path, body, headers or {})
                            response = connection.getresponse()
                            return response.status, json.loads(response.read())
                    auth = {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"}
                    self.assertEqual(request("GET", "/health")[0], 401)
                    self.assertEqual(request("POST", "/review", b"{}", {"Origin": "https://example.org"})[0], 401)
                    self.assertEqual(request("POST", "/review", headers=dict(auth, **{
                        "Content-Length": str(bridge.MAX_BODY + 1)}))[0], 413)
                    self.assertEqual(request("GET", "/health", headers=auth), (200, {"status": "ok", "model": MODEL}))
                    self.assertEqual(request("POST", "/review", json.dumps(PAYLOAD), auth), (
                        200, {"status": "success", "text": "Synthetic feedback", "model": MODEL}))
                    self.assertFalse(staged[0].exists())
                finally:
                    server.shutdown()
                    worker.join()
        driver.review.assert_called_once()


if __name__ == "__main__":
    unittest.main()
