#!/usr/bin/env python3
"""
vertex-api-daemon.py  --  The Ledger's local AI broker  (v2.9, multi-provider
+ direct Anthropic API route + multi-image Silo 2 + Google-optional startup
+ vendor-default sampling + Gemini Developer-API route + Azure OpenAI + desktop handoffs).

[PATCH:google-devapi] v2.9 (WO-A7): the Google route can be provisioned with a
Google AI Studio API key (LEDGER_GOOGLE_API_KEY, "AIza...") INSTEAD of a GCP
project + service account. The key builds genai.Client(api_key=...) against
the Gemini Developer API; everything downstream (caches.create, inline
fallback, usage logging, silo1/silo2) is client-agnostic. If BOTH the key and
GCP_PROJECT_ID are present the key wins and a WARNING is logged -- one Google
route per box. The key is never written to any log line.

[PATCH:no-sampling-params] WO-A2: the daemon sends NO sampling or thinking
parameters (temperature, top_p, top_k, thinking budgets) to any provider, on
any path -- every model runs at its vendor defaults. A "temperature" field in
an incoming /api/silo2 body (the GUI still sends one) is accepted and ignored.

At least one AI provider must be configured. A request for a model whose
provider is unavailable returns an error to the GUI.

The grading GUI (ledger-main-minimus.py) never talks to a provider directly. It
POSTs to this loopback service at http://127.0.0.1:5000, which brokers every
call. Routing is by the model id the GUI sends:

  * Google models  (gemini-*, meta/llama-*, qwen-*, any 'publishers/...' id)
        -> the unified google-genai SDK  (unchanged from v1 / Appendix A)
  * Anthropic models (claude-*, 'anthropic/...', 'publishers/anthropic/...')
        -> [PATCH:direct-api] the DIRECT Anthropic API (anthropic.Anthropic)
           when LEDGER_ANTHROPIC_API_KEY is set in /etc/paragon/.env;
           otherwise the anthropic[vertex] SDK (AnthropicVertex), exactly as
           v2.x. Same messages.create call either way; the GUI cannot tell
           the difference.
  * Azure OpenAI models (gpt-6.1-sol, gpt-6-astra)
        -> Azure OpenAI Responses API using the school tenant's Azure CLI
           sign-in under the daemon service account.
  * Codex models (codex/gpt-6.1-sol, codex/gpt-6-astra)
        -> a private job folder for the signed-in Codex desktop app.
  * Copilot models (copilot/gpt-6.1-sol, copilot/gpt-6-astra, copilot/gpt-5.6-sol)
        -> a loopback bridge to the signed-in Microsoft Copilot desktop app.

[PATCH:multi-image] /api/silo2 additionally accepts an OPTIONAL "images_b64"
array (essay / FULL_PAGE mode: one entry per student response page, in page
order). When present it wins; the legacy single "image_b64" behaves exactly
as before, so the boxed-grid pipeline is byte-path identical. On the Google
path each page becomes its own Part (Gemini tiles each at full resolution);
on the Anthropic path each page becomes its own image block (each page keeps
its own resolution budget instead of sharing one across a tall stitched
stack).

The four-endpoint contract the GUI speaks is IDENTICAL to v1 -- no GUI network
code changes. Caching is abstracted per provider behind that contract:
  * Google    : a server-side context cache (client.caches.create), reused by name.
  * Anthropic : there is no server-side named cache; the daemon remembers the
                system prompt + master images locally and re-sends them as
                cache_control:"ephemeral" blocks on every grade (Anthropic prompt
                caching, 5-min lifetime refreshed on each hit).
  * Azure OpenAI : the daemon holds the instructions and master images in
                   memory and sends them with each grade. It disables Responses
                   API response storage and unwraps structured JSON for the GUI.

Endpoints (all POST, JSON in / JSON out):
  /api/silo1        {model_name, prompt, image_b64}                 -> one stateless vision call
  /api/cache/build  {model_name, system_instruction, images_b64[]} -> establish the grading context
  /api/cache/delete (no body)                                       -> purge the active context
  /api/silo2        {prompt, image_b64 | images_b64[], schema}  ("temperature" ignored)
                                                                     -> grade grid/pages against the context

Success envelope : {"status": "success", "text": "<string>"}
Failure          : HTTP 500 + {"status": "error", "message": "<reason>"}

Config is read from /etc/paragon/.env (root-owned, 0640 root:paragon so the
paragon-run daemon can read it):
  GCP_PROJECT_ID="..."                                      (for the Google Vertex route)
  GCP_LOCATION="global"                                     (optional; default "global")
  GOOGLE_APPLICATION_CREDENTIALS="/etc/paragon/sa.json"     (optional; service-account key)
  LEDGER_DEFAULT_MODEL="gemini-3.5-flash"                   (optional fallback model)
  ANTHROPIC_VERTEX_REGION="global"                          (optional; region for Claude;
                                                             "global", a region like "us-east5",
                                                             or a multi-region "us"/"eu".
                                                             Falls back to GCP_LOCATION.)
  LEDGER_ANTHROPIC_MAX_TOKENS="8192"                        (optional; Claude max output tokens)
  LEDGER_ANTHROPIC_API_KEY="sk-ant-..."                     (optional; when present, Claude
                                                             routes to the DIRECT Anthropic API
                                                             instead of Vertex. Leave unset or
                                                             empty to keep the Vertex route.)
  LEDGER_GOOGLE_API_KEY="AIza..."                           (optional; [PATCH:google-devapi]
                                                             Google AI Studio key -> Gemini
                                                             Developer API. Wins over
                                                             GCP_PROJECT_ID when both are set.)
  LEDGER_AZURE_OPENAI_ENDPOINT="https://<resource>.openai.azure.com"
  LEDGER_AZURE_TENANT_ID="..."                              (school Entra tenant GUID)
  LEDGER_AZURE_OPENAI_TIMEOUT_S="300"                       (optional upstream read timeout)
  LEDGER_AZURE_OPENAI_MAX_OUTPUT_TOKENS="16384"             (optional Azure output ceiling)
  LEDGER_CODEX_ENABLED="true"                             (enable Codex desktop jobs)
  LEDGER_CODEX_JOB_DIR="..."                               (optional absolute jobs folder)
  LEDGER_CODEX_TIMEOUT_S="300"                            (optional Codex job timeout)
  LEDGER_COPILOT_BRIDGE_URL="http://127.0.0.1:8790"       (enable Copilot desktop)
  LEDGER_COPILOT_BRIDGE_TOKEN="..."                       (required for Copilot desktop)
  LEDGER_COPILOT_TIMEOUT_S="320"                          (optional bridge read timeout)

The Vertex routes share the same Google Application Default Credentials.
The Anthropic and Azure Identity SDKs are loaded when their routes need them.
"""

import os
import base64
import logging
import secrets
import sys
import threading
from pathlib import Path

from flask import Flask, request, jsonify
from waitress import create_server, serve

from google import genai
from google.genai import types
from google.genai.errors import APIError

from azure_provider import AZURE_DEFAULT_MODEL, AZURE_MODELS, AzureOpenAIError, AzureOpenAIProvider
from codex_provider import CODEX_DEFAULT_MODEL, CODEX_MODELS, CodexProvider, CodexProviderError
from copilot_provider import (
    COPILOT_DEFAULT_MODEL,
    COPILOT_MODEL_OPTIONS,
    COPILOT_MODELS,
    CopilotProvider,
    CopilotProviderError,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# [PATCH:google-devapi] LEDGER_ENV_PATH is a test seam (tests/ points it at a
# temp file so the startup env-matrix can run without touching /etc/paragon).
# Production never sets it; the systemd unit carries no such variable.
ENV_PATH = os.environ.get("LEDGER_ENV_PATH") or "/etc/paragon/.env"
HOST = "127.0.0.1"
PORT = 5000
_WINDOWS_READY_FILE = os.environ.get("LEDGER_DAEMON_READY_FILE")
_WINDOWS_LOCAL_TOKEN = os.environ.get("LEDGER_DAEMON_LOCAL_TOKEN")
CACHE_TTL = "43200s"                  # 12 hours, matching the GUI's note (Google path)
FALLBACK_MODEL = "gemini-4-argon"   # current Gemini flagship (GA 2026-10-13)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ledger-daemon] %(levelname)s: %(message)s",
)
log = logging.getLogger("ledger-daemon")


# ---------------------------------------------------------------------------
# .env loading  (no python-dotenv dependency; tiny hand-rolled parser)
# ---------------------------------------------------------------------------
def load_env(path):
    """Read KEY=VALUE / KEY="VALUE" lines, ignore blanks and '#' comments, and
    push every value into os.environ so the SDKs can pick up
    GOOGLE_APPLICATION_CREDENTIALS automatically."""
    if not os.path.exists(path):
        raise RuntimeError(
            f"{path} not found. Run the provisioning script (or the setup wizard) first."
        )
    values = {}
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            values[key] = val
            os.environ.setdefault(key, val)
    return values


ENV = load_env(ENV_PATH)
PROJECT_ID = ENV.get("GCP_PROJECT_ID")
LOCATION = ENV.get("GCP_LOCATION") or "global"
DEFAULT_MODEL = ENV.get("LEDGER_DEFAULT_MODEL") or FALLBACK_MODEL
ANTHROPIC_REGION = ENV.get("ANTHROPIC_VERTEX_REGION") or LOCATION
try:
    ANTHROPIC_MAX_TOKENS = int(ENV.get("LEDGER_ANTHROPIC_MAX_TOKENS") or "8192")
except ValueError:
    ANTHROPIC_MAX_TOKENS = 8192

# [PATCH:google-optional] Startup rule: at least ONE provider must be
# configured. [PATCH:google-devapi] Google is configured by GCP_PROJECT_ID OR
# LEDGER_GOOGLE_API_KEY; the direct Anthropic route is configured by
# LEDGER_ANTHROPIC_API_KEY. (The Anthropic key is also resolved again below
# for route selection -- this early peek exists only so the refusal-to-start
# check can run before any client construction.)
_GOOGLE_API_KEY = (ENV.get("LEDGER_GOOGLE_API_KEY") or "").strip()
_HAS_GOOGLE = bool(PROJECT_ID) or bool(_GOOGLE_API_KEY)
_HAS_ANTHROPIC_KEY = bool((ENV.get("LEDGER_ANTHROPIC_API_KEY") or "").strip())
_azure_endpoint = (ENV.get("LEDGER_AZURE_OPENAI_ENDPOINT") or "").strip()
_azure_tenant = (ENV.get("LEDGER_AZURE_TENANT_ID") or "").strip()
_azure_client = None
_azure_config_error = None
if _azure_endpoint or _azure_tenant:
    try:
        _azure_client = AzureOpenAIProvider(
            _azure_endpoint,
            _azure_tenant,
            ENV.get("LEDGER_AZURE_OPENAI_TIMEOUT_S") or "300",
            ENV.get("LEDGER_AZURE_OPENAI_MAX_OUTPUT_TOKENS") or "16384",
        )
    except AzureOpenAIError as exc:
        _azure_config_error = str(exc)
        log.warning("Azure OpenAI is unavailable. %s", _azure_config_error)

_codex_client = None
_codex_config_error = None
if (ENV.get("LEDGER_CODEX_ENABLED") or "").strip().lower() == "true":
    try:
        job_dir = (ENV.get("LEDGER_CODEX_JOB_DIR") or "").strip()
        if not job_dir and _WINDOWS_READY_FILE and os.environ.get("LEDGER_WORKSPACE_PATH"):
            job_dir = str(Path(os.environ["LEDGER_WORKSPACE_PATH"]) / ".Codex Jobs")
        _codex_client = CodexProvider(
            job_dir,
            ENV.get("LEDGER_CODEX_TIMEOUT_S") or "300",
        )
    except CodexProviderError as exc:
        _codex_config_error = str(exc)
        log.warning("Codex desktop is unavailable. %s", _codex_config_error)

_copilot_client = None
_copilot_config_error = None
_copilot_url = (ENV.get("LEDGER_COPILOT_BRIDGE_URL") or "").strip()
if _copilot_url:
    try:
        _copilot_client = CopilotProvider(
            _copilot_url,
            ENV.get("LEDGER_COPILOT_BRIDGE_TOKEN") or "",
            ENV.get("LEDGER_COPILOT_TIMEOUT_S") or "320",
        )
    except CopilotProviderError as exc:
        _copilot_config_error = str(exc)
        log.warning("Copilot desktop is unavailable. %s", _copilot_config_error)

if not (_HAS_GOOGLE or _HAS_ANTHROPIC_KEY or _azure_client or _codex_client or _copilot_client):
    config_error = _azure_config_error or _codex_config_error or _copilot_config_error
    if config_error:
        raise RuntimeError(config_error)
    raise RuntimeError(
        "No AI provider is configured in /etc/paragon/.env. Set "
        "LEDGER_GOOGLE_API_KEY (Google AI Studio key), GCP_PROJECT_ID "
        "(Google Vertex route), LEDGER_ANTHROPIC_API_KEY (direct Anthropic route), "
        "LEDGER_AZURE_OPENAI_ENDPOINT and LEDGER_AZURE_TENANT_ID (Azure OpenAI), "
        "LEDGER_CODEX_ENABLED=true (Codex desktop), "
        "or LEDGER_COPILOT_BRIDGE_URL and LEDGER_COPILOT_BRIDGE_TOKEN (Copilot desktop), "
        "then restart ledger-daemon.service."
    )

_other_cloud_provider = _HAS_GOOGLE or _HAS_ANTHROPIC_KEY
_azure_only = _azure_client is not None and not (_other_cloud_provider or _codex_client or _copilot_client)
_codex_only = _codex_client is not None and not (_other_cloud_provider or _azure_client or _copilot_client)
_copilot_only = _copilot_client is not None and not (_other_cloud_provider or _azure_client or _codex_client)
if not _HAS_GOOGLE and not ENV.get("LEDGER_DEFAULT_MODEL"):
    if _azure_client and not _HAS_ANTHROPIC_KEY:
        DEFAULT_MODEL = AZURE_DEFAULT_MODEL
    elif _codex_client and not _HAS_ANTHROPIC_KEY:
        DEFAULT_MODEL = CODEX_DEFAULT_MODEL
    elif _copilot_only:
        DEFAULT_MODEL = COPILOT_DEFAULT_MODEL
    else:
        DEFAULT_MODEL = "claude-sonnet-5-5"
if _azure_only and DEFAULT_MODEL.lower() not in AZURE_MODELS:
    raise RuntimeError("LEDGER_DEFAULT_MODEL must be gpt-6.1-sol or gpt-6-astra on an Azure OpenAI-only install.")
if _codex_only and DEFAULT_MODEL.lower() not in CODEX_MODELS:
    raise RuntimeError("LEDGER_DEFAULT_MODEL must be codex/gpt-6.1-sol or codex/gpt-6-astra on a Codex-only install.")
if _copilot_only and DEFAULT_MODEL.lower() not in COPILOT_MODELS:
    raise RuntimeError(
        "LEDGER_DEFAULT_MODEL must name a supported Copilot model on a Copilot-only install. "
        f"Supported IDs are {COPILOT_MODEL_OPTIONS}."
    )

# [PATCH:google-devapi] Which Google route this box is using, surfaced on the
# health endpoint: "developer-api" (AI Studio key) | "vertex" | "absent".
if _HAS_GOOGLE:
    # [PATCH:upstream-timeout] bound every Google call so a wedged client cannot hang forever.
    # Override via LEDGER_GOOGLE_TIMEOUT_MS in /etc/paragon/.env (milliseconds). Default 300000 = 5 min.
    _LEDGER_GOOGLE_TIMEOUT_MS = int(ENV.get("LEDGER_GOOGLE_TIMEOUT_MS") or "300000")
    if _GOOGLE_API_KEY:
        # Same hygiene as the Anthropic key: the key value itself is NEVER
        # logged -- only the fact that key-based auth is in use.
        if PROJECT_ID:
            log.warning("Both LEDGER_GOOGLE_API_KEY and GCP_PROJECT_ID are set -- "
                        "the AI Studio key wins; the Vertex project (%s) is ignored. "
                        "One Google route per box.", PROJECT_ID)
        log.info("Initialising Gemini Developer-API (google-genai) client (key-based auth).")
        client = genai.Client(api_key=_GOOGLE_API_KEY,
                              http_options=types.HttpOptions(timeout=_LEDGER_GOOGLE_TIMEOUT_MS))
        _google_route = "developer-api"
    else:
        log.info("Initialising Vertex (google-genai) client (project=%s, location=%s)",
                 PROJECT_ID, LOCATION)
        client = genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION,
                              http_options=types.HttpOptions(timeout=_LEDGER_GOOGLE_TIMEOUT_MS))
        _google_route = "vertex"
    # [PATCH:upstream-timeout-end]
else:
    # Without Google credentials the Google client is never
    # constructed. Every Google-path caller goes through _require_google(),
    # which converts this None into a clear, GUI-surfaced error message.
    client = None
    _google_route = "absent"
    log.info("No Google credentials -- Google route is DORMANT on this box.")


def _require_google():
    """[PATCH:google-optional] Gate for every Google-path call site. Returns the
    live Vertex client, or raises a plain-language error that the standard
    endpoint handlers surface to the GUI as {"status":"error", ...}."""
    if client is None:
        raise RuntimeError(
            "No Google credentials are installed. Select a model for a configured "
            "provider in AI MODEL, or re-initialize with Google credentials to use Gemini."
        )
    return client


def _require_azure():
    if _azure_client is None:
        raise AzureOpenAIError(
            _azure_config_error or "invalid_request: Azure OpenAI requires LEDGER_AZURE_OPENAI_ENDPOINT "
            "and LEDGER_AZURE_TENANT_ID in /etc/paragon/.env."
        )
    return _azure_client


def _require_codex(model_name):
    if model_name.lower() not in CODEX_MODELS:
        raise CodexProviderError("invalid_request: Select codex/gpt-6.1-sol or codex/gpt-6-astra.")
    if _codex_client is None:
        raise CodexProviderError(
            _codex_config_error or "invalid_request: Set LEDGER_CODEX_ENABLED=true to enable Codex desktop."
        )
    return _codex_client


def _require_copilot(model_name):
    if model_name.lower() not in COPILOT_MODELS:
        raise CopilotProviderError(f"invalid_request: Copilot desktop supports {COPILOT_MODEL_OPTIONS}.")
    if _copilot_client is None:
        raise CopilotProviderError(
            _copilot_config_error or "invalid_request: Set LEDGER_COPILOT_BRIDGE_URL and "
            "LEDGER_COPILOT_BRIDGE_TOKEN to enable Copilot desktop."
        )
    return _copilot_client


# The Anthropic client is created lazily on first use (see _anthropic_client),
# so a box without the anthropic SDK installed still serves Gemini normally.
_anthropic_client = None
_anthropic_lock = threading.Lock()
# [PATCH:direct-api] Which Claude route this box is using: "direct-api" when
# LEDGER_ANTHROPIC_API_KEY is present, else "vertex". Resolved once, at client
# construction, and surfaced on the health endpoint for field diagnosis.
# [PATCH:google-devapi] "absent" when neither a key nor a GCP project exists
# (AI-Studio-key-only box): no Claude route can serve, so say so on /.
_ANTHROPIC_API_KEY = (ENV.get("LEDGER_ANTHROPIC_API_KEY") or "").strip()
_anthropic_route = ("direct-api" if _ANTHROPIC_API_KEY
                    else "vertex" if PROJECT_ID else "absent")


def _get_anthropic_client():
    """Construct (once) and return the Claude client for this box's route:
    the DIRECT Anthropic API when LEDGER_ANTHROPIC_API_KEY is set, otherwise
    AnthropicVertex (unchanged v2.x behavior). Both speak the identical
    messages.create contract, so every caller downstream is route-agnostic.
    Raises a clear, GUI-surfaced error if the SDK is not installed."""
    global _anthropic_client
    with _anthropic_lock:
        if _anthropic_client is None:
            # [PATCH:upstream-timeout-anthropic] mirror the bounded timeout on the Claude path.
            # Override via LEDGER_ANTHROPIC_TIMEOUT_S in /etc/paragon/.env (seconds). Default 300.
            _timeout = float(ENV.get("LEDGER_ANTHROPIC_TIMEOUT_S") or "300")
            if _ANTHROPIC_API_KEY:
                try:
                    from anthropic import Anthropic
                except ImportError as exc:
                    raise RuntimeError(
                        "Anthropic models require the 'anthropic' SDK, which is not "
                        "installed in this venv. Install it (pip install anthropic) "
                        "and restart ledger-daemon, or choose a Gemini model."
                    ) from exc
                log.info("Initialising DIRECT Anthropic API client (key-based auth).")
                _anthropic_client = Anthropic(
                    api_key=_ANTHROPIC_API_KEY,
                    timeout=_timeout,
                )
            else:
                # [PATCH:google-devapi] An AI-Studio-key-only box has no GCP
                # project, so AnthropicVertex cannot authenticate. Fail with
                # a GUI-surfaced message instead of a null-project crash.
                if not PROJECT_ID:
                    raise RuntimeError(
                        "Claude models need an Anthropic API key on this box (no "
                        "Google Cloud project is configured, so the Vertex route "
                        "for Claude is unavailable). Add LEDGER_ANTHROPIC_API_KEY "
                        "to /etc/paragon/.env and restart ledger-daemon.service, "
                        "or choose a Gemini model."
                    )
                try:
                    from anthropic import AnthropicVertex
                except ImportError as exc:
                    raise RuntimeError(
                        "Anthropic models require the 'anthropic[vertex]' SDK, which is not "
                        "installed in this venv. Install it (pip install 'anthropic[vertex]') "
                        "and restart ledger-daemon, or choose a Gemini model."
                    ) from exc
                log.info("Initialising AnthropicVertex client (project=%s, region=%s)",
                         PROJECT_ID, ANTHROPIC_REGION)
                _anthropic_client = AnthropicVertex(
                    project_id=PROJECT_ID, region=ANTHROPIC_REGION,
                    timeout=_timeout,
                )
            # [PATCH:upstream-timeout-anthropic-end]
        return _anthropic_client


def _provider_for(model_name):
    """Route a model ID to its provider."""
    m = (model_name or "").lower()
    if m.startswith("codex/"):
        return "codex"
    if m.startswith("copilot/"):
        return "copilot"
    if m in AZURE_MODELS:
        return "azure-openai"
    if m.startswith("claude") or "anthropic/" in m or m.startswith("publishers/anthropic"):
        return "anthropic"
    return "google"


# ---------------------------------------------------------------------------
# Shared context state  (one active grading context at a time)
#   Google    : name = the Vertex cache resource name
#   Anthropic : name = the sentinel "anthropic-local"; the actual reusable
#               material is held in _anthropic_ctx and re-sent each grade.
#   Azure OpenAI and the desktop providers share local masters and instructions.
# ---------------------------------------------------------------------------
_cache_lock = threading.Lock()
_active_cache = {"name": None, "model": None}
_anthropic_ctx = {"system": "", "image_bytes": []}   # raw PNG bytes for master pages
_ANTHROPIC_SENTINEL = "anthropic-local"
# Google inline fallback: when a master is below the model's minimum-cache-token
# floor, caches.create is rejected (400 INVALID_ARGUMENT). We then hold the
# master locally and inline it into each grade -- no cache discount, but small
# masters (e.g. short/single-page demos) still grade. Large masters still cache.
_google_ctx = {"system": "", "image_bytes": []}      # raw PNG bytes for master pages
_GOOGLE_INLINE = "google-inline"
_inline_ctx = {"system": "", "image_bytes": []}
_INLINE_NAMES = {"azure-openai": "azure-openai-inline", "codex": "codex-inline", "copilot": "copilot-inline"}


def _purge_active_cache():
    """Clear the local grading context and delete any named Google cache."""
    with _cache_lock:
        name = _active_cache["name"]
        model = _active_cache["model"]
        _active_cache["name"] = None
        _active_cache["model"] = None
        _anthropic_ctx["system"] = ""
        _anthropic_ctx["image_bytes"] = []
        _google_ctx["system"] = ""
        _google_ctx["image_bytes"] = []
        _inline_ctx["system"] = ""
        _inline_ctx["image_bytes"] = []
    if name and name not in (_ANTHROPIC_SENTINEL, _GOOGLE_INLINE, *_INLINE_NAMES.values()):
        # [PATCH:google-optional] a named Google cache cannot exist on a box
        # with no Vertex client, but guard anyway rather than crash a purge.
        if client is None:
            log.warning("Skipping delete of cache %s: no Google client on this box.", name)
            return
        try:
            client.caches.delete(name=name)
            log.info("Deleted Google context cache: %s", name)
        except APIError as exc:
            log.warning("Could not delete cache %s: %s", name, exc)
    elif name == _ANTHROPIC_SENTINEL:
        log.info("Cleared Anthropic local grading context (model=%s).", model)
    elif name == _GOOGLE_INLINE:
        log.info("Cleared Google inline grading context (model=%s).", model)
    elif name in _INLINE_NAMES.values():
        log.info("Cleared inline grading context (model=%s).", model)


def _is_cache_too_small(exc):
    """True only when a caches.create failure is the minimum-token-floor
    rejection -- the one case we handle by falling back to an inline context.
    Any other error is a real failure and must keep propagating."""
    msg = str(getattr(exc, "message", "") or exc).lower()
    return "minimum token count" in msg or "to start caching" in msg


def _is_explicit_cache_unsupported(exc):
    """[PATCH:implicit-cache-only] True when caches.create is rejected because
    the model only supports IMPLICIT caching (Gemini 4 Argon and onward).
    Both wordings seen in the field: Vertex says 'Model `x` does not support
    explicit context caching.'; AI Studio says 'Explicit caching is not
    supported for models/x'. Match narrowly -- any other error must propagate."""
    msg = str(getattr(exc, "message", "") or exc).lower()
    return ("does not support explicit context caching" in msg
            or "explicit caching is not supported" in msg)


def _log_google_usage(resp, cache_name, per_call_ctx):
    """[PATCH:google-usage-log] The only way to SEE whether Vertex caching
    (explicit or implicit) is working. cached > 0 on every call after the
    first per distinct prefix means the discount is landing. Never raises."""
    try:
        u = getattr(resp, "usage_metadata", None)
        if per_call_ctx:
            path = "per-chunk"
        elif cache_name == _GOOGLE_INLINE:
            path = "inline"
        else:
            path = "cached"
        finish = None
        cands = getattr(resp, "candidates", None) or []
        if cands:
            finish = getattr(cands[0], "finish_reason", None)
        log.info("Google usage: path=%s prompt=%s cached=%s thoughts=%s output=%s finish=%s",
                 path,
                 getattr(u, "prompt_token_count", "?"),
                 getattr(u, "cached_content_token_count", None),
                 getattr(u, "thoughts_token_count", None),
                 getattr(u, "candidates_token_count", "?"),
                 finish)
    except Exception:  # noqa: BLE001 - logging must never kill a grade
        pass


def _decode_png(image_b64):
    """base64 PNG string (with or without a data: URI prefix) -> raw bytes."""
    if not image_b64:
        raise ValueError("image_b64 is empty")
    if image_b64.strip().startswith("data:") and "," in image_b64:
        image_b64 = image_b64.split(",", 1)[1]
    return base64.b64decode(image_b64)


def _anthropic_image_block(png_bytes, cache=False):
    """Build an Anthropic image content block from raw PNG bytes."""
    block = {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": _anthropic_media_type(png_bytes),
            "data": base64.b64encode(png_bytes).decode("ascii"),
        },
    }
    if cache:
        block["cache_control"] = _anthropic_cache_control()
    return block



# [PATCH:payload-governor] Anthropic-route image normalization.
# The Anthropic Messages API caps a request at ~32 MB and server-side
# downscales any image over ~1568 px on the long edge. The master pages
# arrive from the GUI at Matrix(4.16) (~2550x3300 PNG) and are RE-SENT on
# every grade on this route, so a full-booklet master plus a 17-page essay
# packet blew straight past the cap and Anthropic rejected the request
# (HTTP 413) before the model ever saw it. Normalizing here -- and only
# here -- keeps the Google/Gemini path byte-identical while making the
# Anthropic payload ~4-6x smaller with zero fidelity the model could have
# used anyway. Requires Pillow in the daemon venv; falls back to
# passthrough (old behavior) with a loud warning if it is missing.
try:
    _ANTHROPIC_MAX_EDGE = int(ENV.get("LEDGER_ANTHROPIC_MAX_EDGE") or "1568")
except ValueError:
    _ANTHROPIC_MAX_EDGE = 1568
try:
    _ANTHROPIC_JPEG_QUALITY = int(ENV.get("LEDGER_ANTHROPIC_JPEG_QUALITY") or "85")
except ValueError:
    _ANTHROPIC_JPEG_QUALITY = 85
_ANTHROPIC_PAYLOAD_CEILING = 30_000_000  # bytes of base64 image data; API cap is ~32 MB total

# [PATCH:cache-ttl] Prompt-cache lifetime for every cache_control breakpoint
# on the Anthropic route. "1h" (default) or "5m". 1h costs 2x on the write
# but survives the multi-minute gaps between students; 5m costs 1.25x and
# expired between calls in real batches, paying the write premium with no
# reads. Set LEDGER_ANTHROPIC_CACHE_TTL=5m in /etc/paragon/.env to revert.
_ANTHROPIC_CACHE_TTL = (ENV.get("LEDGER_ANTHROPIC_CACHE_TTL") or "1h").strip().lower()
if _ANTHROPIC_CACHE_TTL not in ("1h", "5m"):
    log.warning("LEDGER_ANTHROPIC_CACHE_TTL=%r is not 1h or 5m -- using 1h.", _ANTHROPIC_CACHE_TTL)
    _ANTHROPIC_CACHE_TTL = "1h"


def _anthropic_cache_control():
    """One cache_control dict, honoring the configured TTL."""
    cc = {"type": "ephemeral"}
    if _ANTHROPIC_CACHE_TTL == "1h":
        cc["ttl"] = "1h"
    return cc


def _anthropic_media_type(image_bytes):
    """Sniff JPEG vs PNG from magic bytes so normalized (JPEG) and
    passthrough (PNG) images both carry an honest media_type."""
    if image_bytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    return "image/png"


def _anthropic_normalize_image(image_bytes):
    """Downscale to <=_ANTHROPIC_MAX_EDGE on the long edge and re-encode as
    JPEG q=_ANTHROPIC_JPEG_QUALITY. Anthropic route ONLY."""
    try:
        import io as _io
        from PIL import Image as _PILImage
    except ImportError:
        log.warning("Pillow is not installed in the daemon venv -- Anthropic "
                    "images sent at ORIGINAL size. Large essay packets may "
                    "exceed the 32 MB request cap. Fix: pip install pillow "
                    "in the daemon venv, then restart ledger-daemon.")
        return image_bytes
    try:
        img = _PILImage.open(_io.BytesIO(image_bytes))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        long_edge = max(img.size)
        if _ANTHROPIC_MAX_EDGE > 0 and long_edge > _ANTHROPIC_MAX_EDGE:
            scale = _ANTHROPIC_MAX_EDGE / float(long_edge)
            img = img.resize((max(1, int(img.width * scale)),
                              max(1, int(img.height * scale))),
                             _PILImage.LANCZOS)
        out = _io.BytesIO()
        img.save(out, format="JPEG", quality=_ANTHROPIC_JPEG_QUALITY)
        return out.getvalue()
    except Exception as exc:  # noqa: BLE001 - never let normalization kill a grade
        log.warning("Anthropic image normalization failed (%s); sending "
                    "original bytes.", exc)
        return image_bytes


def _anthropic_text(message):
    """Concatenate the text blocks of an Anthropic Messages response."""
    parts = []
    for block in getattr(message, "content", None) or []:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def _schema_instructions(schema):
    """Turn the GUI's Vertex-style schema into a plain-language JSON instruction
    for Claude (which doesn't take a Gemini response_schema). The GUI already
    strips ```json fences and json_repair-parses, so a firm instruction suffices."""
    base = (
        "Respond with ONLY a JSON array and nothing else -- no prose, no "
        "explanation, no markdown code fences. "
    )
    try:
        items = (schema or {}).get("items", {})
        props = items.get("properties", {})
        if props:
            keys = ", ".join(props.keys())
            base += f"Each array element must be a JSON object with exactly these keys: {keys}."
    except Exception:  # noqa: BLE001 - schema hinting is best-effort
        pass
    return base


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)

if _WINDOWS_READY_FILE:
    if not _WINDOWS_LOCAL_TOKEN:
        raise RuntimeError("The managed Ledger daemon requires a local authorization token.")

    @app.before_request
    def _verify_local_client():
        provided = request.headers.get("Authorization", "")
        if not provided.isascii() or not secrets.compare_digest(provided, f"Bearer {_WINDOWS_LOCAL_TOKEN}"):
            return jsonify({"status": "error", "message": "Unauthorized Ledger client."}), 401


@app.get("/")
def health():
    with _cache_lock:
        cache_active = _active_cache["name"] is not None
        cache_model = _active_cache["model"]
    anthropic_available = False
    try:
        import anthropic  # noqa: F401
        anthropic_available = True
    except ImportError:
        anthropic_available = False
    return jsonify({
        "status": "ok",
        "project": PROJECT_ID,
        "location": LOCATION,
        "default_model": DEFAULT_MODEL,
        "anthropic_region": ANTHROPIC_REGION,
        "anthropic_route": _anthropic_route,   # [PATCH:direct-api] "direct-api" | "vertex"
        "google_route": _google_route,   # [PATCH:google-devapi] "developer-api" | "vertex" | "absent"
        "azure_openai_route": "entra-cli" if _azure_client is not None else "absent",
        **({"azure_openai_default_model": DEFAULT_MODEL} if _azure_only else {}),
        "codex_route": "desktop" if _codex_client is not None else "absent",
        **({"codex_job_dir": str(_codex_client.job_dir)} if _codex_client is not None else {}),
        **({"codex_default_model": DEFAULT_MODEL} if _codex_only else {}),
        "copilot_route": "desktop-bridge" if _copilot_client is not None else "absent",
        **({"copilot_default_model": DEFAULT_MODEL} if _copilot_only else {}),
        "anthropic_sdk_installed": anthropic_available,
        "cache_active": cache_active,
        "cache_model": cache_model,
        "per_chunk_context": True,   # [PATCH:silo2-per-chunk-context] GUI capability gate
    })


# ---------------------------------------------------------------------------
# /api/silo1  -- one stateless vision call (title / student-ID OCR)
# ---------------------------------------------------------------------------
@app.post("/api/silo1")
def silo1():
    try:
        body = request.get_json(force=True) or {}
        model_name = body.get("model_name") or DEFAULT_MODEL
        prompt = body.get("prompt", "")
        png = _decode_png(body.get("image_b64"))
        provider = _provider_for(model_name)

        if provider == "azure-openai":
            text = _require_azure().respond(model_name, prompt, [png])
            return jsonify({"status": "success", "text": text})

        if provider == "codex":
            text = _require_codex(model_name).respond(model_name, prompt, [png])
            return jsonify({"status": "success", "text": text})

        if provider == "copilot":
            text = _require_copilot(model_name).respond(model_name, prompt, [png])
            return jsonify({"status": "success", "text": text})

        if provider == "anthropic":
            ac = _get_anthropic_client()
            msg = ac.messages.create(
                model=model_name,
                max_tokens=ANTHROPIC_MAX_TOKENS,
                messages=[{
                    "role": "user",
                    "content": [
                        _anthropic_image_block(png),
                        {"type": "text", "text": prompt},
                    ],
                }],
            )
            return jsonify({"status": "success", "text": _anthropic_text(msg)})

        # Google path (unchanged from v1)
        image = types.Part.from_bytes(data=png, mime_type="image/png")
        resp = _require_google().models.generate_content(
            model=model_name,
            contents=[image, prompt],
        )
        return jsonify({"status": "success", "text": resp.text or ""})
    except Exception as exc:  # noqa: BLE001 - surface every failure to the GUI
        log.exception("silo1 failed")
        return jsonify({"status": "error", "message": str(exc)}), 500


# ---------------------------------------------------------------------------
# /api/cache/build  -- establish the grading context (system prompt + masters)
# ---------------------------------------------------------------------------
@app.post("/api/cache/build")
def cache_build():
    try:
        body = request.get_json(force=True) or {}
        model_name = body.get("model_name") or DEFAULT_MODEL
        system_instruction = body.get("system_instruction", "")
        images_b64 = body.get("images_b64") or []

        # Never stack contexts: drop any prior one first.
        _purge_active_cache()

        provider = _provider_for(model_name)
        if provider in _INLINE_NAMES:
            if provider == "codex":
                _require_codex(model_name)
            elif provider == "copilot":
                _require_copilot(model_name)
            else:
                _require_azure()
            cache_name = _INLINE_NAMES[provider]
            img_bytes = [_decode_png(b) for b in images_b64]
            with _cache_lock:
                _inline_ctx["system"] = system_instruction
                _inline_ctx["image_bytes"] = img_bytes
                _active_cache["name"] = cache_name
                _active_cache["model"] = model_name
            log.info("Inline grading context staged (model=%s, %d master image(s)).",
                     model_name, len(img_bytes))
            return jsonify({"status": "success", "text": cache_name})

        if provider == "anthropic":
            # No server-side named cache for Anthropic: hold the material locally
            # and re-send it (cache_control'd) on every grade.
            _get_anthropic_client()  # validate SDK presence now, fail early if missing
            # [PATCH:payload-governor] masters normalized ONCE here; every
            # grade re-sends them, so this is where the savings multiply.
            img_bytes = [_anthropic_normalize_image(_decode_png(b)) for b in images_b64]
            with _cache_lock:
                _anthropic_ctx["system"] = system_instruction
                _anthropic_ctx["image_bytes"] = img_bytes
                _active_cache["name"] = _ANTHROPIC_SENTINEL
                _active_cache["model"] = model_name
            log.info("Anthropic grading context staged (model=%s, %d master image(s)).",
                     model_name, len(img_bytes))
            return jsonify({"status": "success", "text": _ANTHROPIC_SENTINEL})

        # Google path. Try a server-side context cache; if the master is below
        # the model's minimum-cache-token floor, fall back to an inline context
        # rather than failing the whole batch. Real multi-page masters still cache.
        img_bytes = [_decode_png(b) for b in images_b64]
        parts = [types.Part.from_bytes(data=b, mime_type="image/png") for b in img_bytes]
        contents = [types.Content(role="user", parts=parts)] if parts else None
        try:
            cache = _require_google().caches.create(
                model=model_name,
                config=types.CreateCachedContentConfig(
                    system_instruction=system_instruction,
                    contents=contents,
                    ttl=CACHE_TTL,
                ),
            )
        except APIError as exc:
            too_small = _is_cache_too_small(exc)
            implicit_only = _is_explicit_cache_unsupported(exc)
            if not (too_small or implicit_only):
                raise
            with _cache_lock:
                _google_ctx["system"] = system_instruction
                _google_ctx["image_bytes"] = img_bytes
                _active_cache["name"] = _GOOGLE_INLINE
                _active_cache["model"] = model_name
            if implicit_only:
                # [PATCH:implicit-cache-only] Argon-class model: no explicit
                # cache exists. Masters ride inline at the FRONT of every
                # call, where Vertex's automatic implicit cache picks them
                # up (>= 4,096-token prefix). Watch 'Google usage: cached='.
                log.info(
                    "Model is implicit-cache-only -- using inline context "
                    "(model=%s, %d master image(s)). Vertex said: %s",
                    model_name, len(parts), exc,
                )
            else:
                log.warning(
                    "Master below cache-token floor -- using inline context "
                    "(model=%s, %d master image(s)). Vertex said: %s",
                    model_name, len(parts), exc,
                )
            return jsonify({"status": "success", "text": _GOOGLE_INLINE})
        with _cache_lock:
            _active_cache["name"] = cache.name
            _active_cache["model"] = model_name
            # [PATCH:silo2-per-chunk-context] keep the system prompt locally too:
            # per-call master-context grades bypass the server cache and need it
            # (until now _google_ctx was only populated on the inline fallback).
            _google_ctx["system"] = system_instruction
        log.info("Google context cache built: %s (model=%s, %d master image(s))",
                 cache.name, model_name, len(parts))
        return jsonify({"status": "success", "text": cache.name})
    except Exception as exc:  # noqa: BLE001
        log.exception("cache/build failed")
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.post("/api/cache/delete")
def cache_delete():
    """Purge the active grading context. (The GUI ignores the response body.)"""
    try:
        _purge_active_cache()
        return jsonify({"status": "success", "text": "context purged"})
    except Exception as exc:  # noqa: BLE001
        log.exception("cache/delete failed")
        return jsonify({"status": "error", "message": str(exc)}), 500


# ---------------------------------------------------------------------------
# /api/silo2  -- grade the stitched grid against the active context
#   Note: the silo2 payload carries NO model_name, so we reuse the model the
#   context was built with -- a Google cache can only be consumed by its own
#   model, and the Anthropic material was staged for one model too.
# ---------------------------------------------------------------------------
@app.post("/api/silo2")
def silo2():
    try:
        body = request.get_json(force=True) or {}
        # [PATCH:no-sampling-params] body["temperature"] is deliberately unread.
        prompt = body.get("prompt", "")
        schema = body.get("schema")
        # [PATCH:multi-image] Essay / FULL_PAGE mode sends "images_b64" (one
        # entry per student response page, in page order). When present it
        # wins; otherwise the legacy single "image_b64" is decoded exactly as
        # before, so the boxed-grid pipeline is untouched.
        imgs_b64 = body.get("images_b64")
        if imgs_b64:
            live_pngs = [_decode_png(b) for b in imgs_b64]
            if not live_pngs:
                raise ValueError("images_b64 is present but empty")
        else:
            live_pngs = [_decode_png(body.get("image_b64"))]
        # [PATCH:silo2-per-chunk-context] OPTIONAL per-call master context. When
        # "master_images_b64" is present, THIS call is graded against exactly
        # those master page(s) instead of the full staged/cached master; when
        # absent, behavior is byte-path identical to before (the fallback).
        master_b64 = body.get("master_images_b64")
        master_pngs = [_decode_png(b) for b in master_b64] if master_b64 else None

        with _cache_lock:
            cache_name = _active_cache["name"]
            cache_model = _active_cache["model"]
            anthropic_system = _anthropic_ctx["system"]
            anthropic_imgs = list(_anthropic_ctx["image_bytes"])
            google_system = _google_ctx["system"]
            google_imgs = list(_google_ctx["image_bytes"])
            inline_system = _inline_ctx["system"]
            inline_imgs = list(_inline_ctx["image_bytes"])
        if not cache_name:
            raise RuntimeError("No active evaluation context. Call /api/cache/build first.")

        provider = _provider_for(cache_model)
        if provider in _INLINE_NAMES:
            if schema is None:
                raise ValueError("invalid_request: This provider requires the Ledger grading schema.")
            masters = master_pngs if master_pngs is not None else inline_imgs
            options = {}
            if provider == "codex":
                provider_client = _require_codex(cache_model)
            elif provider == "copilot":
                provider_client = _require_copilot(cache_model)
            else:
                provider_client = _require_azure()
            if provider != "azure-openai":
                options["master_image_count"] = len(masters)
            text = provider_client.respond(
                cache_model, prompt, [*masters, *live_pngs],
                instructions=inline_system, grading_schema=schema, **options,
            )
            return jsonify({"status": "success", "text": text})

        if provider == "anthropic":
            ac = _get_anthropic_client()
            # [PATCH:payload-governor] live pages get the same normalization
            # the masters got at cache/build.
            live_pngs = [_anthropic_normalize_image(b) for b in live_pngs]
            # [PATCH:silo2-per-chunk-context] per-call masters REPLACE the staged
            # full-master set for THIS call only, normalized like everything else.
            per_call_ctx = master_pngs is not None
            if per_call_ctx:
                anthropic_imgs = [_anthropic_normalize_image(b) for b in master_pngs]
            # System prompt as a cache_control'd system block.
            system_blocks = [{
                "type": "text",
                "text": anthropic_system or "",
                "cache_control": _anthropic_cache_control(),
            }]
            # Master images first (cache the LAST one to mark the whole prefix),
            # then the live grid image and the grading instruction.
            content = []
            last = len(anthropic_imgs) - 1
            for i, b in enumerate(anthropic_imgs):
                # [PATCH:cache-per-call-masters] cache the last master block in
                # BOTH modes. Per-call (S2CTX) masters differ chunk-to-chunk but
                # are identical for the same chunk across every student, so each
                # distinct master set is written once and read on every later
                # student. The old rule left the bulk of the payload uncached.
                content.append(_anthropic_image_block(b, cache=(i == last)))
            # [PATCH:multi-image] one block per live page, in page order
            for b in live_pngs:
                content.append(_anthropic_image_block(b))
            content.append({
                "type": "text",
                "text": f"{prompt}\n\n{_schema_instructions(schema)}",
            })
            # [PATCH:payload-governor] measure what we are about to send and
            # refuse anything the API would 413 anyway -- with a message that
            # names the fix instead of masquerading as a network blip.
            _approx = sum(len(blk["source"]["data"])
                          for blk in content if blk.get("type") == "image")
            log.info("Anthropic silo2 payload: %d image block(s) "
                     "(%d master + %d live), ~%.1f MB base64.",
                     len(anthropic_imgs) + len(live_pngs),
                     len(anthropic_imgs), len(live_pngs), _approx / 1e6)
            if _approx > _ANTHROPIC_PAYLOAD_CEILING:
                raise RuntimeError(
                    f"request_too_large: this evaluation would carry ~{_approx / 1e6:.0f} MB "
                    f"of image data, over the Anthropic 32 MB request cap (HTTP 413). "
                    f"Retrying cannot succeed. Reduce LEDGER_ESSAY_MAX_PAGES_PER_CALL "
                    f"(GUI env) or LEDGER_ANTHROPIC_MAX_EDGE (/etc/paragon/.env).")
            msg = ac.messages.create(
                model=cache_model,
                max_tokens=ANTHROPIC_MAX_TOKENS,
                system=system_blocks,
                messages=[{"role": "user", "content": content}],
            )
            # [PATCH:cache-usage-log] the only way to SEE whether the prefix
            # cache is working. cache_read should be large on every call after
            # the first per distinct master set; cache_write large only then.
            _u = getattr(msg, "usage", None)
            log.info("Anthropic usage: input=%s cache_write=%s cache_read=%s output=%s ttl=%s per_call_ctx=%s",
                     getattr(_u, "input_tokens", "?"),
                     getattr(_u, "cache_creation_input_tokens", "?"),
                     getattr(_u, "cache_read_input_tokens", "?"),
                     getattr(_u, "output_tokens", "?"),
                     _ANTHROPIC_CACHE_TTL, per_call_ctx)
            # [PATCH:truncation-guard] A response that hit the max_tokens
            # ceiling is INCOMPLETE JSON. Returned as-is, json_repair closes
            # the dangling brackets and the GUI commits an amputated grade
            # (missing essays, empty feedback, phantom zeros) with no visible
            # error. Fail LOUDLY instead; the "invalid_request" prefix makes
            # the GUI defer this student immediately with the real reason.
            if getattr(msg, "stop_reason", None) == "max_tokens":
                raise RuntimeError(
                    "invalid_request: model output was TRUNCATED at the "
                    f"{ANTHROPIC_MAX_TOKENS}-token output ceiling before the "
                    "evaluation JSON was complete. The partial evaluation was "
                    "DISCARDED -- a truncated evaluation must never be "
                    "committed. Raise LEDGER_ANTHROPIC_MAX_TOKENS in "
                    "/etc/paragon/.env and restart ledger-daemon.service.")
            return jsonify({"status": "success", "text": _anthropic_text(msg)})

        # Google path.
        # [PATCH:multi-image] one Part per live page/grid, in page order --
        # Gemini tiles each Part natively at its own full resolution.
        live_parts = [
            types.Part.from_bytes(data=b, mime_type="image/png")
            for b in live_pngs
        ]

        # [PATCH:silo2-per-chunk-context] per-call master context: bypass the
        # server cache for THIS call and inline the chunk's own master page(s).
        if master_pngs is not None:
            master_parts = [
                types.Part.from_bytes(data=b, mime_type="image/png")
                for b in master_pngs
            ]
            cfg_kwargs = {}
            if google_system:
                cfg_kwargs["system_instruction"] = google_system
            if schema:
                cfg_kwargs["response_mime_type"] = "application/json"
                cfg_kwargs["response_schema"] = schema
            log.info("Google silo2 per-chunk context: %d master + %d live image(s).",
                     len(master_parts), len(live_parts))
            resp = _require_google().models.generate_content(
                model=cache_model or DEFAULT_MODEL,
                contents=[*master_parts, *live_parts, prompt],
                config=types.GenerateContentConfig(**cfg_kwargs),
            )
            _log_google_usage(resp, cache_name, master_pngs is not None)
            return jsonify({"status": "success", "text": resp.text or ""})

        if cache_name == _GOOGLE_INLINE:
            # No server cache (master was sub-floor): re-send the system prompt +
            # master pages inline alongside the live grid on every grade.
            master_parts = [
                types.Part.from_bytes(data=b, mime_type="image/png")
                for b in google_imgs
            ]
            cfg_kwargs = {}
            if google_system:
                cfg_kwargs["system_instruction"] = google_system
            if schema:
                cfg_kwargs["response_mime_type"] = "application/json"
                cfg_kwargs["response_schema"] = schema
            resp = _require_google().models.generate_content(
                model=cache_model or DEFAULT_MODEL,
                contents=[*master_parts, *live_parts, prompt],
                config=types.GenerateContentConfig(**cfg_kwargs),
            )
            _log_google_usage(resp, cache_name, master_pngs is not None)
            return jsonify({"status": "success", "text": resp.text or ""})

        # Cached path (unchanged from v1 apart from the live-parts splat)
        cfg_kwargs = {"cached_content": cache_name}
        if schema:
            cfg_kwargs["response_mime_type"] = "application/json"
            cfg_kwargs["response_schema"] = schema
        resp = _require_google().models.generate_content(
            model=cache_model or DEFAULT_MODEL,
            contents=[*live_parts, prompt],
            config=types.GenerateContentConfig(**cfg_kwargs),
        )
        _log_google_usage(resp, cache_name, master_pngs is not None)
        return jsonify({"status": "success", "text": resp.text or ""})
    except Exception as exc:  # noqa: BLE001
        log.exception("silo2 failed")
        return jsonify({"status": "error", "message": str(exc)}), 500


if __name__ == "__main__":
    log.info("Google route for this box: %s", _google_route)
    log.info("Claude route for this box: %s", _anthropic_route)
    log.info("Azure OpenAI route for this box: %s", "entra-cli" if _azure_client is not None else "absent")
    log.info("Codex desktop route for this box: %s", "desktop" if _codex_client is not None else "absent")
    log.info("Copilot desktop route for this box: %s", "desktop-bridge" if _copilot_client is not None else "absent")
    if _WINDOWS_READY_FILE:
        # The bound port is read through a private startup file, so the GUI
        # never has to guess whether a local listener belongs to Ledger.
        server = create_server(app, host=HOST, port=0, threads=4)

        def _watch_parent():
            # The GUI owns the write end. EOF also covers a GUI crash.
            sys.stdin.buffer.read(1)
            os._exit(0)

        threading.Thread(target=_watch_parent, daemon=True).start()
        log.info("Ledger AI daemon listening on http://%s:%s", HOST, server.effective_port)
        ready = Path(_WINDOWS_READY_FILE)
        temporary = ready.with_suffix(".tmp")
        temporary.write_text(str(server.effective_port), encoding="ascii")
        temporary.replace(ready)
        server.run()
    else:
        log.info("Ledger AI daemon listening on http://%s:%d", HOST, PORT)
        serve(app, host=HOST, port=PORT, threads=4)
