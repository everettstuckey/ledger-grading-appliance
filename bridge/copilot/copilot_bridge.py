#!/usr/bin/env python3
"""Loopback bridge for an operator-started Microsoft Copilot desktop session."""

import base64
import binascii
import hashlib
import hmac
import http.client
import json
import os
import re
import socket
import struct
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

LOOPBACK = "127.0.0.1"
COPILOT_HOST = "cr.m365copilotapp.svc.cloud.microsoft"
CHAT_PATH = re.compile(r"/hwav2/chat(?:/[A-Za-z0-9_-]+)*/?\Z")
MODEL_LABELS = {
    "copilot/gpt-5.6-sol": "gpt 5.6 sol",
    "copilot/gpt-6.1-sol": "gpt 6.1 sol",
    "copilot/gpt-6-astra": "gpt 6 astra",
}
MAX_PROMPT = 200_000
MAX_IMAGES = 20
MAX_IMAGE_BYTES = 15_000_000
MAX_BASE64 = 60_000_000
MAX_BODY = MAX_BASE64 + 1_500_000
MAX_FRAME = 8_000_000
MAX_PIXELS = 40_000_000
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
ACCOUNT_PATTERN = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_\x60{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\Z")
TARGET_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
REVIEW_LOCK = threading.Lock()


class BridgeError(Exception):
    """A controlled operator message containing no request or account data."""


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    account: str = field(repr=False)
    port: int = 8790
    cdp_port: int = 9223
    timeout: int = 300

    @classmethod
    def from_env(cls, env):
        token = env.get("COPILOT_BRIDGE_TOKEN", "")
        account = env.get("COPILOT_EXPECTED_ACCOUNT", "").strip()
        if not TOKEN_PATTERN.fullmatch(token):
            raise BridgeError("Configure COPILOT_BRIDGE_TOKEN with 32 to 128 URL-safe characters.")
        if not ACCOUNT_PATTERN.fullmatch(account):
            raise BridgeError("Configure COPILOT_EXPECTED_ACCOUNT with the exact work account address.")
        return cls(
            token, account.casefold(),
            _setting_int(env, "COPILOT_BRIDGE_PORT", 8790, 65535),
            _setting_int(env, "COPILOT_CDP_PORT", 9223, 65535),
            _setting_int(env, "COPILOT_TIMEOUT_S", 300, 300),
        )


def _setting_int(env, name, default, maximum):
    try:
        value = int(env.get(name, str(default)))
    except ValueError:
        value = 0
    if not 1 <= value <= maximum:
        raise BridgeError(f"{name} must be between 1 and {maximum}.")
    return value


def _debugger_path(target, port):
    """Treat debugger discovery as a locator, never authority to connect elsewhere."""
    target_id = target.get("id")
    if target.get("type") not in {"webview", "page"}:
        return None
    if not isinstance(target_id, str) or not TARGET_PATTERN.fullmatch(target_id):
        return None
    try:
        page = urlsplit(target.get("url", ""))
        if (page.scheme != "https" or page.netloc != COPILOT_HOST
                or not CHAT_PATH.fullmatch(page.path)):
            return None
        url = urlsplit(target.get("webSocketDebuggerUrl", ""))
        if (url.scheme == "ws" and url.netloc in {f"{LOOPBACK}:{port}", f"localhost:{port}"}
                and url.path == f"/devtools/page/{target_id}" and not url.query and not url.fragment):
            return url.path
    except (TypeError, ValueError):
        pass
    return None


def _remaining(deadline, cap):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BridgeError("Copilot review timed out. Check the desktop app before retrying.")
    return min(remaining, cap)


class MiniWS:
    """Small RFC6455 client pinned to the configured IPv4 loopback port."""

    def __init__(self, port, path, deadline):
        self.sock = socket.create_connection((LOOPBACK, port), _remaining(deadline, 5))
        self.buf = b""
        try:
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            req = (f"GET {path} HTTP/1.1\r\nHost: {LOOPBACK}:{port}\r\nUpgrade: websocket\r\n"
                   "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                   f"Sec-WebSocket-Key: {key}\r\n\r\n")
            self.sock.sendall(req.encode("ascii"))
            while b"\r\n\r\n" not in self.buf:
                self.sock.settimeout(_remaining(deadline, 5))
                chunk = self.sock.recv(4096)
                if not chunk or len(self.buf) + len(chunk) > 16384:
                    raise BridgeError("The local Copilot debugger rejected the connection.")
                self.buf += chunk
            header, _, self.buf = self.buf.partition(b"\r\n\r\n")
            status, *lines = header.split(b"\r\n")
            headers = {name.lower(): value.strip() for name, _, value in
                       (line.partition(b":") for line in lines)}
            expected = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest())
            if (not status.startswith(b"HTTP/1.1 101 ")
                    or headers.get(b"sec-websocket-accept") != expected):
                raise BridgeError("The local Copilot debugger rejected the connection.")
        except BaseException:
            self.close()
            raise

    def close(self):
        self.sock.close()

    def _read_exact(self, count, deadline):
        while len(self.buf) < count:
            self.sock.settimeout(_remaining(deadline, 10))
            chunk = self.sock.recv(min(65536, count - len(self.buf)))
            if not chunk:
                raise BridgeError("The local Copilot debugger disconnected.")
            self.buf += chunk
        out, self.buf = self.buf[:count], self.buf[count:]
        return out

    def _send_frame(self, opcode, payload, deadline):
        mask = os.urandom(4)
        header = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.settimeout(_remaining(deadline, 10))
        self.sock.sendall(header + mask + masked)

    def send_text(self, value, deadline):
        self._send_frame(1, value.encode("utf-8"), deadline)

    def recv_text(self, deadline):
        fragments = bytearray()
        started = False
        while True:
            flags, length = self._read_exact(2, deadline)
            opcode = flags & 0x0F
            if flags & 0x70 or length & 0x80:
                raise BridgeError("The local debugger sent an invalid WebSocket frame.")
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2, deadline))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8, deadline))[0]
            if length > MAX_FRAME - len(fragments):
                raise BridgeError("The local debugger sent an oversized response.")
            data = self._read_exact(length, deadline)
            if opcode == 8:
                raise BridgeError("The local Copilot debugger disconnected.")
            if opcode == 9:
                self._send_frame(10, data, deadline)
            elif opcode != 10:
                if opcode != (0 if started else 1):
                    raise BridgeError("The local debugger sent an invalid WebSocket frame.")
                started = True
                fragments.extend(data)
                if flags & 0x80:
                    return fragments.decode("utf-8")


class CDPSession:
    def __init__(self, port, deadline):
        self.port = port
        self.deadline = deadline
        self.ws = None
        self.message_id = 0

    def connect(self):
        connection = http.client.HTTPConnection(LOOPBACK, self.port, timeout=_remaining(self.deadline, 5))
        try:
            connection.request("GET", "/json/list")
            response = connection.getresponse()
            if response.status != 200:
                raise BridgeError("The local Copilot debugger could not list chat targets.")
            body = response.read(1_000_001)
            if len(body) > 1_000_000:
                raise BridgeError("The local debugger returned too much discovery data.")
            targets = json.loads(body)
        except (OSError, http.client.HTTPException, ValueError):
            raise BridgeError("Start the Copilot desktop app with its loopback debugger and sign in.") from None
        finally:
            connection.close()
        if not isinstance(targets, list):
            raise BridgeError("The local debugger returned invalid target data.")
        paths = [_debugger_path(target, self.port) for target in targets if isinstance(target, dict)]
        paths = [path for path in paths if path]
        if len(paths) != 1:
            raise BridgeError("Open exactly one Microsoft work Copilot chat in the desktop app.")
        self.ws = MiniWS(self.port, paths[0], self.deadline)

    def close(self):
        if self.ws is not None:
            self.ws.close()
            self.ws = None

    def call(self, method, params=None):
        self.message_id += 1
        message_id = self.message_id
        self.ws.send_text(json.dumps({"id": message_id, "method": method, "params": params or {}}),
                          self.deadline)
        while True:
            reply = json.loads(self.ws.recv_text(self.deadline))
            if reply.get("id") == message_id:
                if "error" in reply:
                    raise BridgeError("The local Copilot debugger rejected a chat operation.")
                return reply.get("result", {})

    def evaluate(self, expression, *, by_value=True):
        result = self.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": by_value, "objectGroup": "ledger",
        })
        if "exceptionDetails" in result:
            raise BridgeError("The Copilot page changed or does not expose the expected controls.")
        value = result.get("result", {})
        return value.get("value" if by_value else "objectId")


# Only a visible profile control or its explicitly current account row is identity
# evidence. An address in a prompt, chat response, or inactive account is ignored.
JS_HELPERS = r"""
const visible = e => !!e && e.isConnected && e.getClientRects().length > 0 &&
  getComputedStyle(e).visibility !== 'hidden';
const label = e => (e.getAttribute('aria-label') || '').trim();
const buttons = (root = document) =>
  [...root.querySelectorAll('button, [role="button"]')].filter(visible);
const named = (pattern, root = document) => buttons(root).find(e => pattern.test(label(e)));
const normalize = value => (value || '').replace(/\s+/g, ' ').trim();
const composer = () => {
  const editors = [...document.querySelectorAll('#chat-input, #m365-chat-editor-target-element')]
    .filter(e => visible(e) && (e.isContentEditable || e.matches('textarea,input[type="text"]')));
  return editors.length === 1 ? editors[0] : null;
};
const draft = edit => {
  const text = typeof edit.value === 'string' ? edit.value : edit.innerText;
  return edit.id === 'm365-chat-editor-target-element' ? text.replace(/^[\u200b\u200c]+|[\u200b\u200c]+$/g, '') : text;
};
const workAccountControl = () => {
  const matches = [...document.querySelectorAll('[data-testid="nav-footer"] div[role="button"]')]
    .filter(e => visible(e) && /\bwork account$/i.test(label(e)));
  return matches.length === 1 ? matches[0] : null;
};
const accountControl = () => workAccountControl() || named(
  /^(account manager\b|account switcher\b|your account\b|profile menu\b|manage account\b|user profile\b|signed in as\b)/i);
const openDialogs = () => [...document.querySelectorAll('[role="dialog"]')].filter(visible);
const workAccountCard = dialogs => {
  if (dialogs.length !== 1) return null;
  const [dialog] = dialogs;
  if (dialog.getAttribute('aria-modal') !== 'true' || !/\baccount\b/i.test(label(dialog))) return null;
  const signOut = buttons(dialog).filter(e => /^sign out$/i.test(normalize(e.innerText)));
  if (signOut.length !== 1) return null;
  const group = signOut[0].closest('[role="group"]');
  if (!group || !dialog.contains(group)) return null;
  return [...group.children].find(e => e.contains(signOut[0])) || null;
};
const accountMenuState = button => {
  if (button !== workAccountControl()) return button.getAttribute('aria-expanded');
  const dialogs = openDialogs();
  if (dialogs.length === 0) return 'false';
  return workAccountCard(dialogs) ? 'true' : null;
};
const textParts = e => [e.innerText || '', label(e), e.getAttribute('title') || '',
  ...((e.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean)
    .map(id => document.getElementById(id)).filter(visible).map(d => d.innerText || ''))];
const emails = parts => [...new Set(parts.join('\n').toLowerCase()
  .match(/[a-z0-9.!#$%&'*+/=?^_|{}~\x60-]+@[a-z0-9.-]+\.[a-z]{2,}/g) || [])];
const hasWork = parts => parts.some(part => part.split(/\r?\n/).some(
  line => /^(work|school|work account|school account|work or school account)$/i.test(line.trim())));
const profileParts = button => {
  const profile = button.closest('[data-testid="account-profile"],[data-testid="user-profile"]') ||
    button.parentElement;
  return profile && (profile.innerText || '').length < 250 ? textParts(profile) : textParts(button);
};
const legacyAccountParts = button => {
  const menuId = button.getAttribute('aria-controls') || button.getAttribute('aria-owns');
  const menu = menuId && document.getElementById(menuId);
  const selected = visible(menu) ? [...menu.querySelectorAll(
    '[data-testid="active-account"],[data-testid="current-account"],' +
    '[role="menuitemradio"][aria-checked="true"],[role="option"][aria-selected="true"],' +
    '[role="menuitem"][aria-current="true"]')].filter(visible) : [];
  if (selected.length > 1) return [];
  const parts = [...textParts(button), ...profileParts(button), ...selected.flatMap(textParts)];
  return hasWork(parts) ? parts : [];
};
const accountVerified = () => {
  const button = accountControl();
  if (!button) return false;
  let parts;
  if (button === workAccountControl()) {
    const card = workAccountCard(openDialogs());
    if (!card) return false;
    parts = textParts(card);
  } else {
    parts = legacyAccountParts(button);
  }
  const identities = emails(parts);
  if (identities.length !== 1 || identities[0] !== expectedAccount) return false;
  window[guardKey] = [button, label(button), button.getAttribute('title'), normalize(button.innerText)];
  return true;
};
const accountGuard = () => {
  const button = accountControl(), saved = window[guardKey];
  if (!button || !saved || saved[0] !== button || saved[1] !== label(button) ||
      saved[2] !== button.getAttribute('title') || saved[3] !== normalize(button.innerText)) return false;
  if (button === workAccountControl() && openDialogs().length) return false;
  const parts = [...textParts(button), ...profileParts(button)], identities = emails(parts);
  const personal = parts.some(part => part.split(/\r?\n/).some(
    line => /^(personal|personal account)$/i.test(line.trim())));
  // The current row may be the only place that showed the Work marker.
  // Once the menu closes, require the same control and reject contradictory evidence.
  return !personal && (!identities.length || (identities.length === 1 && identities[0] === expectedAccount));
};
const modelButton = () => {
  const button = document.querySelector('#gptModeSwitcher');
  return visible(button) ? button : null;
};
const modelLabel = () => {
  const b = modelButton();
  return b ? normalize((label(b) || b.innerText).replace(/,\s*Model Selector$/i, '')) : '';
};
const newChat = () => [...document.querySelectorAll('button,a,[role="button"],[role="link"]')]
  .find(e => visible(e) && /^(new chat|start a new chat)$/i.test(label(e) || normalize(e.innerText)));
const turnRole = e => {
  const author = e.getAttribute('data-message-author-role'), type = e.getAttribute('data-testid');
  if (author === 'user' || type === 'user-message') return 'user';
  if (author === 'assistant' || type === 'assistant-message') return 'assistant';
  if (e.matches('h1,h2,h3,h4,h5,h6,[role="heading"]')) {
    const value = normalize(e.textContent);
    if (/^you said:?$/i.test(value)) return 'user';
    if (/^copilot said:?$/i.test(value)) return 'assistant';
  }
  return '';
};
const turns = () => {
  const anchors = [...document.querySelectorAll(
    '[data-message-author-role],[data-testid="user-message"],[data-testid="assistant-message"],' +
    'h1,h2,h3,h4,h5,h6,[role="heading"]')].filter(e => turnRole(e));
  const distinct = anchors.filter(e => !anchors.some(parent =>
    parent !== e && parent.contains(e) && turnRole(parent) === turnRole(e)));
  const edit = composer();
  return distinct.map(anchor => {
    let container = anchor;
    for (let n = 0; n < 7 && container.parentElement; n++) {
      const parent = container.parentElement;
      if (parent === document.body || (edit && parent.contains(edit)) ||
          distinct.some(other => other !== anchor && parent.contains(other))) break;
      container = parent;
    }
    return { role: turnRole(anchor), container };
  });
};
const composerGroup = () => {
  const edit = composer();
  const limit = edit?.id === 'm365-chat-editor-target-element' ? 10 : 7;
  const profile = accountControl(), chatStart = newChat(), currentTurns = turns();
  let match = null;
  for (let group = edit, n = 0; group && n < limit; group = group.parentElement, n++) {
    if (group === document.body || (profile && group.contains(profile)) ||
        (chatStart && group.contains(chatStart)) ||
        currentTurns.some(t => group.contains(t.container))) break;
    if (group.querySelector('#upload-file-button[type="file"]')) match = group;
  }
  return match;
};
const attachmentCount = () => {
  const group = composerGroup();
  return group ? buttons(group).filter(b => /^Remove attachment(?:\b|:)/i.test(label(b))).length : -1;
};
const onNewRoute = () => /^\/hwav2\/chat\/?$/.test(location.pathname) && !location.hash &&
  ![...new URLSearchParams(location.search).keys()].some(k =>
    k !== 'M365ChatFeatures' && /(?:conversation|thread|chat|session)/i.test(k));
const emptyChat = () => {
  const edit = composer();
  return !!edit && onNewRoute() && !normalize(draft(edit)) && attachmentCount() === 0 &&
    turns().length === 0 && !named(/^(copy response|try again|stop generating)$/i);
};
const lastReply = prompt => {
  const current = turns();
  if (current.length !== 2 || current[0].role !== 'user' || current[1].role !== 'assistant') return null;
  const user = current[0].container, assistant = current[1].container;
  const userMessage = user.querySelector('[data-testid="user-message-content"],[data-testid="message-content"]');
  const sent = normalize(prompt);
  const userText = userMessage || user;
  const received = normalize(userText.innerText || userText.textContent);
  const matchesPrompt = userMessage ? received === sent : received.includes(sent);
  if (!matchesPrompt || !named(/^(copy|copy response)$/i, assistant) ||
      !named(/^try again\b/i, assistant) || named(/^stop(?: generating)?$/i)) return null;
  const message = assistant.querySelector(
    '[data-testid="assistant-message-content"],[data-testid="message-content"],.markdown');
  return message ? message.innerText.trim() :
    assistant.innerText.replace(/^\s*copilot said:?\s*/i, '').trim();
};
"""


def _label_id(value):
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.casefold().split()).replace("gpt-", "gpt ")
    for model_id, name in MODEL_LABELS.items():
        if normalized in {name, name + " think", name + " think deeper", name + " quick response"}:
            return model_id
    return None


class ChatDriver:
    def __init__(self, settings, deadline):
        self.settings = settings
        self.deadline = deadline
        self.session = CDPSession(settings.cdp_port, deadline)
        self.guard_key = "__ledgerAccount_" + os.urandom(12).hex()

    def __enter__(self):
        try:
            self.session.connect()
            return self
        except BaseException:
            self.session.close()
            raise

    def __exit__(self, *_):
        try:
            self._js("delete window[guardKey]; return null;")
        except Exception:
            pass
        finally:
            self.session.close()

    def _js(self, body, *, by_value=True):
        return self.session.evaluate(
            "(() => { if (location.protocol !== 'https:' || location.port !== '' ||"
            f" location.hostname !== {json.dumps(COPILOT_HOST)} ||"
            r" !/^\/hwav2\/chat(?:\/[A-Za-z0-9_-]+)*\/?$/.test(location.pathname)) return null;"
            f"const expectedAccount = {json.dumps(self.settings.account)};"
            f"const guardKey = {json.dumps(self.guard_key)};"
            + JS_HELPERS + body + "\n})()", by_value=by_value,
        )

    def _pause(self, seconds=0.5):
        time.sleep(_remaining(self.deadline, seconds))

    def _wait(self, predicate, seconds, message):
        end = min(self.deadline, time.monotonic() + seconds)
        while time.monotonic() < end:
            if predicate():
                return
            self._pause()
        raise BridgeError(message)

    def _assert_account(self):
        if self._js("return accountVerified();") is True:
            return
        opened = self._js("""
          const b = accountControl();
          if (!b || accountMenuState(b) !== 'false') return false;
          b.click();
          return true;
        """)
        if opened is True:
            try:
                self._wait(lambda: self._js("return accountVerified();") is True, 4,
                           "The configured Microsoft work account was not verified.")
                return
            finally:
                self._js("""
                  const b = accountControl();
                  if (b && accountMenuState(b) === 'true') b.click();
                  return null;
                """)
        raise BridgeError("The configured Microsoft work account was not verified.")

    def _select_model(self, model_id):
        if _label_id(self._js("return modelLabel();")) == model_id:
            return
        if self._js("const b = modelButton(); if (!b) return false; b.click(); return true;") is not True:
            raise BridgeError("The Copilot model picker is unavailable.")

        def options():
            return self._js("""
              return [...document.querySelectorAll('[role="menuitem"],[role="menuitemradio"],[role="option"]')]
                .filter(visible).map(e => normalize(e.innerText)).filter(t => t && t.length < 80);
            """) or []

        def matching(labels):
            candidates = [item for item in labels if _label_id(item) == model_id]
            return next((item for item in candidates if item.casefold().endswith("think deeper")),
                        candidates[0] if candidates else None)

        self._pause(0.7)
        picked = matching(options())
        if picked is None:
            self._js(r"""
              const provider = [...document.querySelectorAll('[role="menuitem"]')].filter(visible)
                .find(e => /^gpt(?:(?:\s*\|\s*|\s+)openai)?$/i.test(normalize(e.innerText)));
              if (provider) provider.click();
              return null;
            """)
            self._pause(0.7)
            picked = matching(options())
        if picked is None or self._js("""
          const picked = [...document.querySelectorAll('[role="menuitem"],[role="menuitemradio"],[role="option"]')]
            .filter(visible).find(e => normalize(e.innerText) === """ + json.dumps(picked) + """);
          if (!picked) return false;
          picked.click();
          return true;
        """) is not True:
            raise BridgeError("The requested model is unavailable in the signed-in Copilot model picker.")
        self._wait(lambda: _label_id(self._js("return modelLabel();")) == model_id, 5,
                   "The requested Copilot model could not be verified.")

    def health(self):
        self._assert_account()
        return {"status": "ok", "model": _label_id(self._js("return modelLabel();"))}

    def review(self, prompt, image_files, model_id):
        self._assert_account()
        if self._js("const b = newChat(); if (!b || !accountGuard()) return false;"
                    " b.click(); return true;") is not True:
            raise BridgeError("The New chat control was not found. No review was sent.")
        self._wait(lambda: self._js("return emptyChat();") is True, 12,
                   "A fresh empty Copilot chat could not be verified. No review was sent.")
        self._assert_account()
        self._select_model(model_id)
        self._assert_account()

        # The file-input handle is scoped to this document. Navigation invalidates it.
        model_label = self._js("return modelLabel();")
        if _label_id(model_label) != model_id:
            raise BridgeError("The requested Copilot model could not be verified before upload.")
        handle = self._js("""
          if (!emptyChat() || !accountGuard() || modelLabel() !== """ + json.dumps(model_label) + """)
            return null;
          return composerGroup().querySelector('#upload-file-button[type="file"]');
        """, by_value=False)
        if not handle:
            raise BridgeError("The verified fresh Copilot chat does not expose its attachment input.")
        self.session.call("DOM.setFileInputFiles", {"files": image_files, "objectId": handle})
        self._wait(lambda: self._js("return attachmentCount();") == len(image_files), 30,
                   "Copilot did not confirm the requested page attachments.")

        self._assert_account()
        model_label = self._js("return modelLabel();")
        if _label_id(model_label) != model_id:
            raise BridgeError("The requested Copilot model changed before submission.")
        prompt_js = json.dumps(prompt)
        file_count = str(len(image_files))
        prepared = self._js("""
          const edit = composer(), prompt = """ + prompt_js + """;
          const ready = () => !!edit && composer() === edit && accountGuard() && onNewRoute() &&
            !normalize(draft(edit)) && !turns().length && attachmentCount() === """ + file_count + """ &&
            modelLabel() === """ + json.dumps(model_label) + """;
          if (!ready()) return false;
          edit.focus();
          if (document.activeElement !== edit || !ready()) return false;
          if (edit.id !== 'm365-chat-editor-target-element') return document.execCommand('insertText', false, prompt);
          document.addEventListener('beforeinput', event => {
            if (event.data === prompt && (event.target !== edit || document.activeElement !== edit || !ready())) {
              event.preventDefault();
              event.stopImmediatePropagation();
            }
          }, {capture: true, signal: AbortSignal.timeout(1500)});
          return 'insert-text';
        """)
        if prepared == "insert-text":
            self.session.call("Input.insertText", {"text": prompt})
            self._pause()
        elif prepared is not True:
            raise BridgeError("Copilot did not accept the prompt in the fresh chat.")

        # Click at most once. A retry after an ambiguous result could duplicate the send.
        send = """
          const edit = composer();
          if (!edit || !accountGuard() || !onNewRoute() || turns().length ||
              normalize(draft(edit)) !== normalize(""" + prompt_js + """) ||
              modelLabel() !== """ + json.dumps(model_label) + """ ||
              attachmentCount() !== """ + file_count + """) return null;
          const matches = buttons(composerGroup()).filter(e => /^(submit|send)(?: message)?$/i.test(label(e)));
          if (matches.length !== 1) return false;
          const [button] = matches;
          if (button.disabled || button.getAttribute('aria-disabled') === 'true') return false;
          button.click();
          return true;
        """
        def submit():
            result = self._js(send)
            if result is None:
                raise BridgeError("The Copilot chat changed before the prompt could be sent.")
            return result is True

        self._wait(submit, 35, "The Copilot send control did not become ready.")
        self._wait(lambda: self._js("return turns().length > 0 || !composer() || !normalize(draft(composer()));") is True,
                   12, "Copilot did not confirm submission of the draft.")
        previous = None
        while time.monotonic() < self.deadline:
            current = self._js("return (accountGuard() || accountVerified()) ? lastReply(" +
                               prompt_js + ") : null;")
            if isinstance(current, str) and 1 < len(current) < 500_000 and current == previous:
                return current
            previous = current
            self._pause(1)
        raise BridgeError("No completed Copilot response could be matched to the submitted turn.")


def _image_header(data):
    if (len(data) >= 45 and data[:16] == b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            and data.endswith(b"\x00\x00\x00\x00IEND\xaeB\x60\x82")
            and struct.unpack(">I", data[29:33])[0] == (binascii.crc32(data[12:29]) & 0xffffffff)):
        width, height = struct.unpack(">II", data[16:24])
        suffix = ".png"
    elif data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9"):
        suffix = ".jpg"
        width, height = 0, 0
        i = 2
        while i + 8 < len(data) and data[i] == 255:
            marker = data[i + 1]
            if marker == 255:
                i += 1
                continue
            size = int.from_bytes(data[i + 2:i + 4], "big")
            if size < 2 or i + size + 2 > len(data) or marker in {0xDA, 0xD9}:
                break
            if marker in {0xC0, 0xC1, 0xC2} and size >= 8:
                height, width = struct.unpack(">HH", data[i + 5:i + 9])
                break
            i += size + 2
    else:
        raise BridgeError("Every page attachment must be a PNG or JPEG image.")
    if not (1 <= width <= 12000 and 1 <= height <= 12000 and width * height <= MAX_PIXELS):
        raise BridgeError("A page image has invalid or oversized dimensions.")
    return suffix


def _parse_review(body):
    try:
        request = json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        raise BridgeError("Invalid JSON request.") from None
    if not isinstance(request, dict) or set(request) != {"prompt", "images_b64", "model"}:
        raise BridgeError("Review requires prompt, images_b64, and model.")
    prompt, images, model = request["prompt"], request["images_b64"], request["model"]
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT:
        raise BridgeError("Review prompt is empty, invalid, or too long.")
    if not isinstance(model, str) or model not in MODEL_LABELS:
        raise BridgeError("Unknown Copilot model ID.")
    if (not isinstance(images, list) or not 1 <= len(images) <= MAX_IMAGES
            or not all(isinstance(item, str) and item for item in images)
            or sum(map(len, images)) > MAX_BASE64):
        raise BridgeError("Provide between 1 and 20 bounded base64 page images.")
    decoded = []
    for image in images:
        if len(image) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
            raise BridgeError("A page image exceeds the size limit.")
        try:
            data = base64.b64decode(image, validate=True)
        except (ValueError, binascii.Error):
            raise BridgeError("A page image is not valid base64.") from None
        if len(data) > MAX_IMAGE_BYTES:
            raise BridgeError("A page image exceeds the size limit.")
        decoded.append((_image_header(data), data))
    return prompt, decoded, model


class BridgeHandler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(10)
        super().setup()

    def log_message(self, *_):
        pass

    def _json(self, code, payload):
        data = json.dumps(payload).encode("utf-8")
        self.close_connection = True
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, code, message):
        self._json(code, {"status": "error", "message": message})

    def _authorized(self):
        headers = self.headers.get_all("Authorization") or []
        if (self.headers.get("Origin") is None and len(headers) == 1
                and headers[0].isascii() and len(headers[0]) <= 135
                and hmac.compare_digest(headers[0], "Bearer " + self.server.settings.token)):
            return True
        self._error(401, "Unauthorized.")
        return False

    def do_GET(self):
        if not self._authorized():
            return
        if self.path != "/health":
            return self._error(404, "Unknown endpoint.")
        if not REVIEW_LOCK.acquire(blocking=False):
            return self._error(409, "Copilot bridge is busy.")
        try:
            settings = self.server.settings
            with ChatDriver(settings, time.monotonic() + min(10, settings.timeout)) as driver:
                result = driver.health()
        except BridgeError as exc:
            return self._error(503, str(exc))
        except Exception:
            return self._error(503, "Copilot desktop is unavailable.")
        finally:
            REVIEW_LOCK.release()
        return self._json(200, result)

    def do_POST(self):
        settings = self.server.settings
        deadline = time.monotonic() + settings.timeout
        if not self._authorized():
            return
        if self.path != "/review":
            return self._error(404, "Unknown endpoint.")
        lengths = self.headers.get_all("Content-Length") or []
        if (self.headers.get("Transfer-Encoding") is not None or len(lengths) != 1
                or re.fullmatch(r"[0-9]{1,8}", lengths[0]) is None):
            return self._error(400, "Invalid request length.")
        length = int(lengths[0])
        if not 0 < length <= MAX_BODY:
            return self._error(413, "Request exceeds the size limit.")
        if self.headers.get_content_type() != "application/json":
            return self._error(415, "Expected application/json.")
        if not REVIEW_LOCK.acquire(blocking=False):
            return self._error(409, "Copilot bridge is busy.")
        try:
            try:
                body = bytearray()
                while len(body) < length:
                    self.request.settimeout(_remaining(deadline, 10))
                    chunk = self.rfile.read1(min(65536, length - len(body)))
                    if not chunk:
                        raise BridgeError("Incomplete JSON request.")
                    body.extend(chunk)
                prompt, images, model_id = _parse_review(body)
            except (BridgeError, OSError) as exc:
                message = str(exc) if isinstance(exc, BridgeError) else "Could not read the request."
                return self._error(400, message)

            with tempfile.TemporaryDirectory(prefix="ledger-copilot-") as directory:
                image_files = []
                for i, (suffix, data) in enumerate(images, 1):
                    _remaining(deadline, settings.timeout)
                    path = Path(directory) / f"page-{i}{suffix}"
                    path.write_bytes(data)
                    image_files.append(str(path))
                with ChatDriver(settings, deadline) as driver:
                    text = driver.review(prompt, image_files, model_id)
            result = {"status": "success", "text": text, "model": model_id}
        except BridgeError as exc:
            return self._error(502, str(exc))
        except Exception:
            return self._error(502, "Copilot desktop review failed.")
        finally:
            REVIEW_LOCK.release()
        return self._json(200, result)


class BridgeServer(ThreadingHTTPServer):
    def __init__(self, settings):
        self.settings = settings
        self.slots = threading.BoundedSemaphore(4)
        super().__init__((LOOPBACK, settings.port), BridgeHandler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # Do not emit tracebacks containing app URLs, account details or scans.
        pass


def _probe(settings):
    with ChatDriver(settings, time.monotonic() + min(10, settings.timeout)) as driver:
        model = driver.health()["model"]
    if model not in MODEL_LABELS:
        raise BridgeError("The work account was verified, but the selected model is unsupported or unavailable.")
    print(f"Copilot desktop is ready. Selected model: {model}.", flush=True)


def main():
    args = sys.argv[1:]
    if args not in ([], ["--probe"]):
        raise SystemExit("Usage: python copilot_bridge.py [--probe]")
    try:
        settings = Settings.from_env(os.environ)
        if args:
            _probe(settings)
            return
    except BridgeError as exc:
        raise SystemExit(str(exc)) from None
    except Exception:
        raise SystemExit("Copilot desktop is unavailable. Check the local debugger and work sign-in.") from None
    with BridgeServer(settings) as server:
        print(f"Ledger Copilot bridge listening on {LOOPBACK}:{settings.port}.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
