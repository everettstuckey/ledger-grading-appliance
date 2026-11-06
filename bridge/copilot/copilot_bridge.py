#!/usr/bin/env python3
"""
copilot_bridge.py -- Windows-side Copilot review bridge for The Ledger.

THE PROBLEM THIS SOLVES
  The Ledger appliance (Debian) scores scanned student work by sending page
  images to a cloud AI provider. Google (Gemini) and Anthropic (Claude) are
  reached with API keys. Microsoft Copilot has no such key: the licensed
  Copilot experience (with GPT 6.1 Sol in its model picker) lives in the
  Microsoft Copilot desktop app, behind an interactive sign-in.

WHAT THIS BRIDGE IS
  A small local HTTP service, run on the Windows machine where the Microsoft
  Copilot desktop app is installed and signed in. The Ledger daemon's COPILOT
  route POSTs a scoring request here; this bridge drives the REAL Copilot
  app over the Chrome DevTools Protocol and returns the response text.

  It does NOT modify Microsoft's code, does NOT extract or store tokens or
  cookies, does NOT read conversation history, and does NOT run a separate
  browser or profile. It attaches to the app's own signed-in session by
  launching the app with a DevTools debugging port and scripting its UI --
  the same attach-to-existing-browser pattern the appliance uses elsewhere.

HOW THE APP IS ATTACHED
  copilotapp.exe is an Edge-family Chromium binary. Launching it with
    --remote-debugging-port=<port> --user-data-dir=<its own default data dir>
  exposes the standard DevTools endpoint on 127.0.0.1. (The explicit
  --user-data-dir is REQUIRED by modern Chromium: it refuses the debugging
  port when the data dir is left implicit. The path passed is the app's OWN
  default, so no second profile is created.) If the app is already running
  without the port, the bridge restarts it (chats and sign-in live on disk in
  the user-data-dir, so a restart is non-destructive).

REQUEST CONTRACT  (what the Ledger daemon calls)
  GET  /health   -> {"status":"ok","model":"GPT 6.1 Sol","app":true,...}
  POST /review   -> {"prompt":"...", "images_b64":["..."], (allowed_models)}
                 <- {"status":"success","text":"..."} |
                    {"status":"error","message":"..."}

  Images are base64 PNG/JPEG page scans. Each request runs in a FRESH chat so
  students are never scored against each other's context.

MODEL POLICY ("GPT 6.1 Sol or higher")
  Before every request the bridge reads the app's model picker. If the
  selected model is not allowed it tries to select the first allowed model
  present in the picker, then re-verifies. A request whose model cannot be
  verified FAILS CLOSED -- it is never silently downgraded. Default allowed
  set: "GPT 6.1 Sol", "GPT 6 Astra" (the GPT-6.1-Sol-or-better entries in
  Microsoft's Copilot model list).

CONFIG (environment variables; all optional)
  COPILOT_BRIDGE_PORT     local HTTP port for this service      (default 8790)
  COPILOT_CDP_PORT        the app's DevTools port               (default 9223)
  COPILOT_APP_EXE         full path to copilotapp.exe           (default: auto-detect)
  COPILOT_USER_DATA_DIR   the app's user-data-dir               (default: auto-detect)
  COPILOT_ALLOWED_MODELS  comma-separated picker labels         (default below)
  COPILOT_TIMEOUT_S       per-request overall timeout            (default 300)
  COPILOT_DEBUG           1 -> verbose logging                   (default 0)

USAGE
  python copilot_bridge.py            run the service (foreground)
  python copilot_bridge.py --probe    one diagnostic pass, no HTTP service

Requires: Python 3.9+ (Windows), standard library only.
The Ledger is EXPERIMENTAL EVALUATION SOFTWARE -- see DISCLAIMER.md. This
bridge inherits that status and its limits.
"""

import base64
import json
import os
import re
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BRIDGE_VERSION = "1.0.0"


def _env_int(name, default):
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


BRIDGE_PORT = _env_int("COPILOT_BRIDGE_PORT", 8790)
CDP_PORT = _env_int("COPILOT_CDP_PORT", 9223)
APP_EXE = os.environ.get("COPILOT_APP_EXE") or (
    r"C:\Program Files (x86)\Microsoft\Copilot\Application\copilotapp.exe")
USER_DATA_DIR = os.environ.get("COPILOT_USER_DATA_DIR") or os.path.join(
    os.environ.get("LOCALAPPDATA", ""), r"Microsoft\Copilot\User Data")
ALLOWED_MODELS = [m.strip() for m in
                  (os.environ.get("COPILOT_ALLOWED_MODELS") or
                   "GPT 6.1 Sol,GPT 6 Astra").split(",") if m.strip()]
TIMEOUT_S = _env_int("COPILOT_TIMEOUT_S", 300)
DEBUG = os.environ.get("COPILOT_DEBUG") == "1"

REVIEW_LOCK = threading.Lock()   # the chat UI handles one conversation at a time


def log(msg, *args):
    print("[copilot-bridge] " + (msg % args if args else msg), flush=True)


def dlog(msg, *args):
    if DEBUG:
        log("DEBUG " + (msg % args if args else msg))


class BridgeError(Exception):
    """Operator-readable failure; the message goes back to the Ledger GUI."""


# --------------------------------------------------------------------------
# Minimal RFC6455 WebSocket client (ws://127.0.0.1 only, no extensions).
# CDP needs a WebSocket; this keeps the bridge dependency-free.
# --------------------------------------------------------------------------
class MiniWS:
    def __init__(self, host, port, path):
        import socket
        self.sock = socket.create_connection((host, port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key))
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket handshake failed (no response)")
            resp += chunk
        head, _, rest = resp.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError("WebSocket handshake rejected: " +
                               head.split(b"\r\n")[0].decode(errors="replace"))
        self.buf = rest

    def _read_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("WebSocket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _read_frame(self):
        head = self._read_exact(2)
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read_exact(8))[0]
        if masked:
            mask = self._read_exact(4)
        payload = self._read_exact(length)
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if opcode == 0x9:              # ping -> pong
            self._send_frame(0xA, payload)
            return self._read_frame()
        if opcode == 0xA:              # pong
            return self._read_frame()
        return opcode, payload

    def _send_frame(self, opcode, payload):
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
        self.sock.sendall(header + mask + masked)

    def send_text(self, text):
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_text(self):
        while True:
            opcode, payload = self._read_frame()
            if opcode == 0x1:
                return payload.decode("utf-8", errors="replace")
            if opcode == 0x8:
                raise RuntimeError("WebSocket closed by peer")

    def close(self):
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------
# CDP session on the chat target
# --------------------------------------------------------------------------
class CDPSession:
    """One DevTools session against the Copilot app's chat page."""

    def __init__(self, cdp_port=CDP_PORT):
        self.cdp_port = cdp_port
        self.ws = None
        self.msg_id = 0
        self.pending = {}

    def _http_json(self, path):
        url = "http://127.0.0.1:%d%s" % (self.cdp_port, path)
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))

    def cdp_alive(self):
        try:
            self._http_json("/json/version")
            return True
        except Exception:
            return False

    def find_chat_target(self):
        for t in self._http_json("/json"):
            if t.get("type") == "webview" and "/chat" in (t.get("url") or ""):
                return t
        for t in self._http_json("/json"):
            if (t.get("url") or "").startswith("https://") and "/chat" in t["url"]:
                return t
        return None

    def connect(self, timeout=60):
        """Attach to the chat page (caller ensures the app is running)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.cdp_alive():
                target = self.find_chat_target()
                if target:
                    m = re.match(r"ws://([^:/]+):(\d+)(.*)",
                                 target["webSocketDebuggerUrl"])
                    if m:
                        self.ws = MiniWS(m.group(1), int(m.group(2)), m.group(3))
                        threading.Thread(target=self._reader, daemon=True).start()
                        return target
            time.sleep(1.0)
        raise BridgeError(
            "Copilot app: no chat page reachable on DevTools port %d. "
            "Start the app (signed in) and retry." % self.cdp_port)

    def _reader(self):
        try:
            while True:
                msg = json.loads(self.ws.recv_text())
                if "id" in msg and msg["id"] in self.pending:
                    self.pending[msg["id"]]["response"] = msg
                    self.pending[msg["id"]]["event"].set()
        except Exception:
            for slot in self.pending.values():
                slot["event"].set()

    def call(self, method, params=None, timeout=None):
        self.msg_id += 1
        mid = self.msg_id
        slot = {"event": threading.Event(), "response": None}
        self.pending[mid] = slot
        self.ws.send_text(json.dumps(
            {"id": mid, "method": method, "params": params or {}}))
        if not slot["event"].wait(timeout or TIMEOUT_S):
            raise BridgeError("Copilot app: DevTools call %s timed out." % method)
        resp = slot["response"] or {}
        self.pending.pop(mid, None)
        if "error" in resp:
            raise BridgeError("Copilot app: DevTools %s failed: %s"
                              % (method, resp["error"].get("message")))
        return resp.get("result", {})

    def evaluate(self, expression, await_promise=False):
        """Runtime.evaluate returning the JS value (or None)."""
        r = self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
        }, timeout=90)
        if r.get("exceptionDetails"):
            detail = r["exceptionDetails"].get("exception", {}).get("description", "")
            raise BridgeError("Copilot app: page script failed: %s" % detail[:300])
        return (r.get("result") or {}).get("value")

    def set_files(self, css_selector, file_paths):
        """DOM.setFileInputFiles on the composer's <input type=file>."""
        doc = self.call("DOM.getDocument", {"depth": 0})
        node = self.call("DOM.querySelector", {
            "nodeId": doc["root"]["nodeId"], "selector": css_selector})
        if not node.get("nodeId"):
            raise BridgeError(
                "Copilot app: file input %s not found on the chat page "
                "(UI update?)." % css_selector)
        self.call("DOM.setFileInputFiles", {
            "files": file_paths, "nodeId": node["nodeId"]})


# --------------------------------------------------------------------------
# App lifecycle
# --------------------------------------------------------------------------
def _app_running():
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq copilotapp.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15).stdout
        return "copilotapp.exe" in out.lower()
    except Exception:
        return False


def ensure_app_with_cdp():
    """Make sure the Copilot app is running WITH the DevTools port open.

    Chromium only honors --remote-debugging-port when --user-data-dir is also
    explicit, so the app's OWN default data dir is passed verbatim -- no
    second profile is ever created. If the app is already up without the
    port it is restarted (chats and sign-in persist on disk).
    """
    probe = CDPSession(CDP_PORT)
    if probe.cdp_alive():
        return "already-running"
    was_running = _app_running()
    if was_running:
        log("Copilot app is running without the DevTools port -- restarting it "
            "(chats and sign-in persist).")
        subprocess.run(["taskkill", "/IM", "copilotapp.exe", "/F"],
                       capture_output=True, timeout=30)
        subprocess.run(["taskkill", "/IM", "copilotapphost.exe", "/F"],
                       capture_output=True, timeout=30)
        time.sleep(3)
    if not os.path.exists(APP_EXE):
        raise BridgeError(
            "Copilot app not found at %s . Install the Microsoft Copilot "
            "desktop app (see bridge/copilot/README.md) or set COPILOT_APP_EXE."
            % APP_EXE)
    log("Launching Copilot app with DevTools port %d ...", CDP_PORT)
    subprocess.Popen(
        [APP_EXE, "--remote-debugging-port=%d" % CDP_PORT,
         '--user-data-dir=%s' % USER_DATA_DIR],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) |
        getattr(subprocess, "DETACHED_PROCESS", 0))
    deadline = time.time() + 90
    while time.time() < deadline:
        if probe.cdp_alive():
            return "launched"
        time.sleep(2)
    raise BridgeError(
        "Copilot app did not expose its DevTools port within 90s. Open the "
        "app once by hand (sign in if asked), close it, and retry.")


# --------------------------------------------------------------------------
# Chat driver
# --------------------------------------------------------------------------
JS_HELPERS = r"""
window.__ledger = {
  findComposer() {
    return document.querySelector('[contenteditable="true"][role="textbox"][aria-label="Message Copilot"]')
        || document.querySelector('[contenteditable="true"][role="textbox"]');
  },
  findModelButton() {
    return [...document.querySelectorAll("button, [role='button'], [role='combobox']")]
      .find(b => /,\s*Model Selector$/i.test(b.getAttribute("aria-label") || ""));
  },
  currentModel() {
    const b = window.__ledger.findModelButton();
    if (!b) return null;
    const a = b.getAttribute("aria-label") || "";
    return a.replace(/,\s*Model Selector$/i, "").trim();
  },
  findNewChat() {
    return [...document.querySelectorAll("button, a, [role='button'], [role='link']")]
      .find(b => /^new chat$/i.test((b.getAttribute("aria-label") || "").trim())
                || /^new chat$/i.test((b.textContent || "").trim()));
  },
  findSubmit() {
    return [...document.querySelectorAll("button, [role='button']")]
      .find(b => /^(submit|send)\b/i.test((b.getAttribute("aria-label") || "").trim()));
  },
  findStop() {
    return [...document.querySelectorAll("button, [role='button']")]
      .find(b => /^stop\b/i.test((b.getAttribute("aria-label") || "").trim()));
  },
  findTryAgain() {
    // Appears only once the response has finished generating.
    return [...document.querySelectorAll("button, [role='button']")]
      .find(b => /^try again/i.test((b.getAttribute("aria-label") || "").trim()));
  },
  copyButtons() {
    // "Copy Response" -- one per finished assistant response.
    return [...document.querySelectorAll("button, [role='button']")]
      .filter(b => /^copy\b/i.test((b.getAttribute("aria-label") || "").trim()));
  },
  responseText(nthFromEnd) {
    const btns = window.__ledger.copyButtons();
    if (!btns.length) return null;
    const btn = btns[Math.max(0, btns.length - 1 - (nthFromEnd || 0))];
    const composer = window.__ledger.findComposer();
    // Preferred: the response turn -- the closest ancestor whose text carries
    // the "Copilot said" header but NOT the user's turn above it.
    let el = btn;
    for (let i = 0; i < 10 && el.parentElement; i++) {
      el = el.parentElement;
      const t = (el.innerText || "").trim();
      if (/copilot said/i.test(t)) {
        if (/you said/i.test(t)) break;      // walked too far up (transcript)
        if (composer && el.contains(composer)) break;
        return t.replace(/^[\s\S]*?copilot said:?[\s\n]*/i, "").trim();
      }
    }
    // Fallback: largest short ancestor that does not contain the composer.
    el = btn; let best = null;
    for (let i = 0; i < 10 && el; i++) {
      el = el.parentElement;
      if (!el || (composer && el.contains(composer))) break;
      const t = (el.innerText || "").trim();
      if (t.length > 40) best = el;
      if (t.length > 400) break;
    }
    return best ? best.innerText.trim() : null;
  }
};
"""


class ChatDriver:
    """Drives the signed-in Copilot chat page over CDP."""

    def __init__(self):
        self.session = CDPSession(CDP_PORT)

    def connect(self):
        ensure_app_with_cdp()
        target = self.session.connect()
        self.session.evaluate(JS_HELPERS)
        return target

    def _require(self, js, what):
        val = self.session.evaluate("!!(%s)" % js)
        if not val:
            raise BridgeError(
                "Copilot app: %s not found on the chat page -- the app UI "
                "may have updated, or the page is still loading/sign-in is "
                "required. Open the app window once to check." % what)

    # -- model policy --------------------------------------------------------
    def current_model(self):
        return self.session.evaluate("window.__ledger.currentModel()")

    def _picker_entries(self):
        """Open the picker and return its visible entry texts.

        The menu expands lazily: it first shows effort modes plus provider
        categories (e.g. 'GPT | OpenAI'); the actual model entries render
        only after the category is opened. So: open the picker, list; when
        no entry matches an allowed model, open the provider category whose
        text contains the allowed model's first word (e.g. 'GPT'), wait, and
        list again.
        """
        self.session.evaluate(
            "(() => { const b = window.__ledger.findModelButton();"
            " if (b) b.click(); return true; })()")
        time.sleep(1.5)
        entries = self.session.evaluate(
            "[...document.querySelectorAll('[role=\"menuitem\"],"
            " [role=\"menuitemradio\"], [role=\"option\"]')]"
            ".map(e => (e.innerText || '').replace(/\\s*\\n+\\s*/g, ' ').trim())"
            ".filter(t => t && t.length < 80)") or []
        return entries

    def _expand_provider(self, family_word):
        """Click the provider category (e.g. 'GPT | OpenAI') to reveal its
        model entries. Returns (clicked, re-listed entries)."""
        clicked = self.session.evaluate(
            "(() => { const els = [...document.querySelectorAll("
            "'[role=\"menuitem\"]')];"
            " const el = els.find(e => ((e.innerText || '').trim()"
            ".toLowerCase().indexOf(%r) === 0));"
            " if (el) { el.click(); return true; } return false; })()"
            % family_word.lower())
        time.sleep(2.0)
        entries = self.session.evaluate(
            "[...document.querySelectorAll('[role=\"menuitem\"],"
            " [role=\"menuitemradio\"], [role=\"option\"]')]"
            ".map(e => (e.innerText || '').replace(/\\s*\\n+\\s*/g, ' ').trim())"
            ".filter(t => t && t.length < 80)") or []
        return clicked is True, entries

    def _close_picker(self):
        self.session.evaluate(
            "(() => { document.body.dispatchEvent(new KeyboardEvent('keydown',"
            "{key: 'Escape', bubbles: true})); return true; })()")
        time.sleep(0.5)

    @staticmethod
    def _label_allowed(label, allowed):
        return any(w.lower() in (label or "").lower() for w in allowed)

    def ensure_model(self, allowed, prefer_effort="deeper"):
        """Verify the chat's model is one of `allowed`; if not, select one.

        Matching is by containment: a picker entry matches when it CONTAINS
        an allowed model string (e.g. "GPT 6.1 Sol" matches the entry
        "GPT 6.1 Sol Think deeper"). Among matches, "Think deeper" (higher
        reasoning effort -- better for grading) is preferred by default.
        Fails CLOSED when no allowed model can be selected.
        """
        model = self.current_model()
        if model is None:
            raise BridgeError("Copilot app: model selector not found -- the "
                              "signed-in chat page did not load. Check the "
                              "app window and your sign-in/license.")
        if self._label_allowed(model, allowed):
            return model
        dlog("model is %r (not allowed) -- opening picker", model)
        entries = self._picker_entries()
        try:
            matches = [e for e in entries if self._label_allowed(e, allowed)]
            if not matches:
                # Lazily-expanded menu: open the provider category named by
                # the leading word of the first allowed model (e.g. 'GPT'
                # from 'GPT 6.1 Sol' -> the 'GPT | OpenAI' submenu).
                head = re.match(r"[A-Za-z]+", allowed[0].split()[0])
                family = head.group(0) if head else allowed[0].split()[0]
                clicked, entries = self._expand_provider(family)
                dlog("provider expand (%s): clicked=%s entries=%s",
                     family, clicked, entries)
                matches = [e for e in entries if self._label_allowed(e, allowed)]
            picked = None
            if matches:
                key = ("think deeper" if prefer_effort == "deeper"
                       else "quick response" if prefer_effort == "quick" else None)
                picked = next((m for m in matches
                               if key and key in m.lower()), matches[0])
            if not picked:
                raise BridgeError(
                    "Copilot model check FAILED CLOSED: this account's model "
                    "picker currently offers none of %s (it shows: %s). "
                    "GPT 6.1 Sol is rolling out in phases -- wait for it on "
                    "this account, or widen COPILOT_ALLOWED_MODELS "
                    "deliberately."
                    % (", ".join(allowed),
                       "; ".join(e for e in entries if e)[:200] or "nothing"))
            clicked = self.session.evaluate(
                "(() => { const els = [...document.querySelectorAll("
                "'[role=\"menuitem\"], [role=\"menuitemradio\"],"
                " [role=\"option\"]')];"
                " const el = els.find(e => ((e.innerText || '')"
                ".replace(/\\s*\\n+\\s*/g, ' ').trim()) === %r);"
                " if (el) { el.click(); return true; } return false; })()"
                % picked)
            if clicked is not True:
                raise BridgeError(
                    "Copilot app: could not click the model-picker entry %r."
                    % picked)
        finally:
            self._close_picker()
        time.sleep(1.0)
        model = self.current_model()
        if not self._label_allowed(model, allowed):
            raise BridgeError(
                "Copilot model check FAILED CLOSED: selection is %r, required "
                "one of %s. The request was NOT sent."
                % (model, ", ".join(allowed)))
        return model

    # -- one review ----------------------------------------------------------
    def review(self, prompt, image_files, allowed, timeout):
        """Attach images, submit prompt in a fresh chat, return response text."""
        started = time.time()

        # Fresh chat so no student's work leaks into another's context.
        if self.session.evaluate("!!window.__ledger.findNewChat()"):
            self.session.evaluate(
                "(() => { window.__ledger.findNewChat().click(); return true; })()")
            time.sleep(2.0)
        else:
            dlog("no New chat button found; continuing in the current chat")

        model = self.ensure_model(allowed)
        dlog("model verified: %r", model)

        # Attach the page images to the composer's file input.
        if image_files:
            self._require("document.querySelector('#upload-file-button')",
                          "the file-attach input")
            self.session.set_files("#upload-file-button", image_files)
            self._wait_attachments(len(image_files), 30)

        # Baseline of existing responses, then type and submit.
        baseline = self.session.evaluate("window.__ledger.copyButtons().length") or 0
        composer_set = self.session.evaluate(
            "(() => { const ed = window.__ledger.findComposer(); if (!ed) return false;"
            " ed.focus();"
            " document.execCommand('selectAll', false, null);"
            " document.execCommand('delete', false, null);"
            " return document.execCommand('insertText', false, %r); })()" % prompt)
        if composer_set is not True:
            raise BridgeError(
                "Copilot app: could not place the scoring prompt into the "
                "composer (chat page not ready or UI changed).")
        time.sleep(0.5)
        # Submit. The Send button stays DISABLED while attachments upload,
        # so retry until it is enabled, click, and CONFIRM the composer
        # actually cleared -- that is the real proof the prompt went out.
        sent = False
        submit_deadline = time.time() + 45
        while time.time() < submit_deadline:
            clicked = self.session.evaluate(
                "(() => { const b = window.__ledger.findSubmit();"
                " if (b && !b.disabled && b.getAttribute('aria-disabled') !== 'true')"
                " { b.click(); return true; } return false; })()")
            if clicked is True:
                time.sleep(1.2)
                remaining = self.session.evaluate(
                    "(() => { const ed = window.__ledger.findComposer();"
                    " return ed ? (ed.innerText || '').trim().length : 0; })()")
                if not remaining:
                    sent = True
                    break
            time.sleep(0.8)
        if not sent:
            # Last resort: Enter inside the composer, then re-verify.
            self.session.evaluate(
                "(() => { const ed = window.__ledger.findComposer(); if (!ed) return false;"
                " for (const t of ['keydown', 'keypress', 'keyup'])"
                "   ed.dispatchEvent(new KeyboardEvent(t, {key: 'Enter', code: 'Enter',"
                "     keyCode: 13, which: 13, bubbles: true, cancelable: true}));"
                " return true; })()")
            time.sleep(1.5)
            remaining = self.session.evaluate(
                "(() => { const ed = window.__ledger.findComposer();"
                " return ed ? (ed.innerText || '').trim().length : 0; })()")
            if remaining:
                raise BridgeError(
                    "Copilot app: the prompt (with its page images) could not "
                    "be submitted -- the Send button never became active. "
                    "Check the app window: the attachment may still be "
                    "uploading or was rejected.")
        dlog("submitted; waiting for the response")

        text = self._wait_response(baseline, timeout - (time.time() - started))
        if not text or len(text.strip()) < 2:
            raise BridgeError(
                "Copilot app: the response came back empty. The model may have "
                "refused the request -- check the app window's last chat.")
        return text

    def _wait_attachments(self, count, timeout):
        """Wait until `count` attachment previews exist in the composer area."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            n = self.session.evaluate(
                "(() => { const ed = window.__ledger.findComposer(); if (!ed) return 0;"
                " let box = ed; for (let i = 0; i < 4 && box.parentElement; i++)"
                "   box = box.parentElement;"
                " return box.querySelectorAll('img, [class*=\"ttachment\" i]').length; })()")
            if n and n >= count:
                return
            time.sleep(0.5)
        raise BridgeError(
            "Copilot app: page images did not attach within %ds (wanted %d). "
            "Check the app window." % (timeout, count))

    def _wait_response(self, baseline_copies, timeout):
        """Wait for the next assistant response and return its text.

        Completion signals, all required together:
          * a NEW Copy button exists (one per finished assistant response),
          * the Stop button is gone (or never appeared -- short responses
            finish between polls),
          * a "Try Again" button exists (rendered only when generation
            completes),
        and then the extracted text must be stable across two polls.
        """
        deadline = time.time() + max(30, timeout)
        last_text, stable_since = None, None
        while time.time() < deadline:
            stop = self.session.evaluate("!!window.__ledger.findStop()")
            try_again = self.session.evaluate("!!window.__ledger.findTryAgain()")
            copies = self.session.evaluate("window.__ledger.copyButtons().length") or 0
            if (not stop and try_again and copies > baseline_copies):
                text = self.session.evaluate("window.__ledger.responseText(0)")
                if text is not None:
                    if text == last_text:
                        if stable_since and time.time() - stable_since >= 2.0:
                            return text
                        elif not stable_since:
                            stable_since = time.time()
                    else:
                        last_text, stable_since = text, None
            time.sleep(1.0)
        raise BridgeError(
            "Copilot app: no completed response within the timeout. The app "
            "window may show an error or a sign-in prompt.")


# --------------------------------------------------------------------------
# HTTP service
# --------------------------------------------------------------------------
class BridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):           # quiet default access log
        if DEBUG:
            log("HTTP " + (fmt % args))

    def _json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path not in ("/health", "/health/"):
            return self._json(404, {"status": "error", "message": "unknown path"})
        try:
            driver = ChatDriver()
            driver.connect()
            model = driver.current_model()
            app = _app_running()
            return self._json(200, {
                "status": "ok",
                "bridge_version": BRIDGE_VERSION,
                "app": bool(app),
                "model": model,
                "allowed_models": ALLOWED_MODELS,
                "timeout_s": TIMEOUT_S,
            })
        except BridgeError as exc:
            return self._json(200, {"status": "error", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001
            return self._json(200, {"status": "error", "message": str(exc)})

    def do_POST(self):
        if self.path not in ("/review", "/review/"):
            return self._json(404, {"status": "error", "message": "unknown path"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception as exc:  # noqa: BLE001
            return self._json(400, {"status": "error", "message": "bad JSON: %s" % exc})
        prompt = (body.get("prompt") or "").strip()
        images_b64 = body.get("images_b64") or []
        allowed = [m for m in (body.get("allowed_models") or ALLOWED_MODELS) if m]
        if not prompt:
            return self._json(400, {"status": "error",
                                    "message": "prompt is required"})
        if len(prompt) > 200_000:
            return self._json(400, {"status": "error",
                                    "message": "prompt too large (>200k chars)"})
        total = sum(len(b or "") for b in images_b64)
        if total > 60_000_000:  # base64 chars; ~45 MB binary
            return self._json(400, {
                "status": "error",
                "message": ("request_too_large: %d images (~%.0f MB) exceed the "
                            "Copilot bridge limit. Reduce pages per call."
                            % (len(images_b64), total / 1.35e6))})

        with REVIEW_LOCK:
            tmp_files = []
            try:
                for i, b64 in enumerate(images_b64):
                    data = base64.b64decode((b64 or "").split(",")[-1])
                    ext = ".jpg" if data[:3] == b"\xff\xd8\xff" else ".png"
                    path = os.path.join(os.environ.get("TEMP", "."),
                                        "ledger-copilot-%d-%d%s"
                                        % (os.getpid(), i, ext))
                    with open(path, "wb") as fh:
                        fh.write(data)
                    tmp_files.append(path)
                driver = ChatDriver()
                driver.connect()
                text = driver.review(prompt, tmp_files, allowed, TIMEOUT_S)
                log("review ok (%d image(s), %d chars back)",
                    len(tmp_files), len(text))
                return self._json(200, {"status": "success", "text": text})
            except BridgeError as exc:
                log("review failed: %s", exc)
                return self._json(200, {"status": "error", "message": str(exc)})
            except Exception as exc:  # noqa: BLE001
                log("review crashed: %s", exc)
                return self._json(200, {"status": "error", "message": str(exc)})
            finally:
                for path in tmp_files:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass


def probe():
    """Diagnostic pass: app, CDP, chat target, model, allowed-set verdict."""
    print("Copilot bridge probe")
    print("  app exe        :", APP_EXE, "(exists: %s)" % os.path.exists(APP_EXE))
    print("  user data dir  :", USER_DATA_DIR)
    print("  app running    :", _app_running())
    state = ensure_app_with_cdp()
    print("  devtools port  :", CDP_PORT, "(%s)" % state)
    driver = ChatDriver()
    target = driver.connect()
    print("  chat target    :", (target.get("url") or "")[:100])
    model = driver.current_model()
    print("  current model  :", model)
    verdict = ("ALLOWED" if ChatDriver._label_allowed(model, ALLOWED_MODELS)
               else "NOT in allowed set")
    print("  model verdict  : %s (allowed: %s)" % (verdict, ", ".join(ALLOWED_MODELS)))
    composer = driver.session.evaluate("!!window.__ledger.findComposer()")
    fileinput = driver.session.evaluate("!!document.querySelector('#upload-file-button')")
    print("  composer       :", composer, " file input:", fileinput)
    if model not in ALLOWED_MODELS:
        print("  NOTE: run one review (or set it by hand) to switch the model; "
              "the bridge switches it automatically per request.")


def main():
    if "--probe" in sys.argv:
        probe()
        return
    server = ThreadingHTTPServer(("127.0.0.1", BRIDGE_PORT), BridgeHandler)
    log("Copilot bridge v%s listening on http://127.0.0.1:%d "
        "(CDP port %d, allowed models: %s)",
        BRIDGE_VERSION, BRIDGE_PORT, CDP_PORT, ", ".join(ALLOWED_MODELS))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("stopped")


if __name__ == "__main__":
    main()

