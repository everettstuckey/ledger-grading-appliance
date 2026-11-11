from tkinter import ttk
import os
import time
import logging
import threading
import tkinter as tk
from tkinter import filedialog, messagebox
import customtkinter as ctk
import fitz  # PyMuPDF
from PIL import Image, ImageStat
import cv2  # NEW: OpenCV for cropping
import numpy as np # NEW: Matrix math for OpenCV
import pytesseract # NEW: Local OCR engine
import json
import json_repair  # SURGICAL INSERTION: The forgiving parser
import csv
import sqlite3
import subprocess
import shutil
import sys
import tempfile
import re
import base64
import io
import datetime
import random  # [PATCH:silo2-bounded-retry] full-jitter backoff
import requests
import decimal
import math
import numbers
from urllib.parse import urlencode


# --- [PATCH:score-format] ---------------------------------------------------
# Summing model-returned points in binary floating point yields values like
# 3.5999999999999996, which then reached teachers verbatim in the conspectus,
# Tabula and gradebook. Every score shown to a human goes through this ONE
# helper: at most two decimals, trailing zeros trimmed (3.6, 3.55, 3 -- never
# a forced ".00"). Ties round half-up on the value's shortest repr, so 2.675
# shows as 2.68 the way a teacher would round it. FORMAT TIME ONLY: stored
# values, DB writes and grading math never see the rounded string.
# Non-numeric input ("-", None, "DEFERRED") passes through as str(value).
def format_score(value):
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        return str(value)
    if not isinstance(value, numbers.Integral) and not math.isfinite(value):
        return str(value)  # nan / inf: show as-is rather than crash a report
    try:
        d = decimal.Decimal(str(float(value))) if not isinstance(value, numbers.Integral) \
            else decimal.Decimal(int(value))
        d = d.quantize(decimal.Decimal("0.01"), rounding=decimal.ROUND_HALF_UP)
    except (decimal.InvalidOperation, ValueError, OverflowError):
        return str(value)
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s == "-0" else s
# --- end [PATCH:score-format] -----------------------------------------------


# --- [PATCH:overflow-detector] ----------------------------------------------
# xelatex exits 0 even when an unbreakable box is taller than the page body
# and runs off the paper (the feedback tail is silently lost). This scans a
# compile .log for "Overfull \vbox (NNNpt too high)" lines above a small
# threshold so the batch log can warn the teacher. WARN-ONLY by contract:
# the detector never blocks, alters or fails a PDF, and every call site must
# swallow its exceptions.
def detect_vbox_overflows(log_text, threshold_pt=5.0):
    """Return the list of Overfull \\vbox heights (pt) above threshold_pt."""
    hits = re.findall(r"Overfull \\vbox \(([0-9.]+)pt too high\)", log_text)
    out = []
    for h in hits:
        try:
            v = float(h)
        except ValueError:
            continue
        if v > threshold_pt:
            out.append(v)
    return out
# --- end [PATCH:overflow-detector] --------------------------------------------


# --- [PATCH:boxed-digit-ocr] -----------------------------------------------
# Unified OCR for Ledger's white-digit-in-dark-box marks (footer page number).
# Never crops-and-inverts a rectangle: the box's own dark rim would invert
# into a black stroke flush against the digits, which tesseract reads as
# '|' / '[' / ']' / '0'. Instead, fill the box contour into a mask and take
# the digits as the LIGHT pixels INSIDE it. The rim is dark, so it can never
# enter the crop.
#
# [PATCH:boxed-digit-ocr-v3] (Rev jjj) Grayscale-first + multi-PSM vote.
# v2 failed on thin-stroke serif footers (LibreOffice / Liberation Serif,
# Gilded Age Test: 12/16, incl. SILENT '16'->'1'): binarizing at native
# resolution + a 2x2 MORPH_OPEN deleted ~1 px strokes, and the fixed 9 px
# mask erosion clipped digits near the rim of small boxes. v3:
#   1. mask erosion is box-proportional (never > box/8, floor 2 px)
#   2. digits stay GRAYSCALE through the upscale; Otsu binarization happens
#      only at target height, followed by CLOSE (heals hairline breaks;
#      never OPEN, which amputates thin strokes)
#   3. speck removal by connected-component area, not morphology
#   4. tesseract votes across several PSMs; winner = highest per-word
#      confidence, agreement count breaks ties (fixes the chronic '11'->'1'
#      weakness of lone PSM 8, all fonts)
# Validation: 16/16 real Gilded Age crops; 384/384 synthetic (Liberation
# Serif + Liberation Sans Bold, box heights 55/90/140 px, clean + scan-
# degraded, values 1-40). v2 baseline on same corpus: 12/16 real, latent
# silent misreads 5->3, 14->4, 21->27 on small serif.
#
# Tunables (env, all optional):
#   LEDGER_DIGIT_ERODE_PX   inner-margin CAP, px    (default 9; actual erode
#                                                    = min(cap, box/8), >= 2)
#   LEDGER_DIGIT_TARGET_H   upscale target height   (default 96)
#   LEDGER_DIGIT_PAD_PX     white quiet zone, px    (default 25)
#   LEDGER_DIGIT_PSM_LIST   comma list of PSMs to vote (default "7,8,6")

_BOXED_DIGIT_WL = "-c tessedit_char_whitelist=0123456789"


def _read_boxed_digits(gray, contour, thresh, lo=None, hi=None):
    """Return (value:int|None, raw:str).

    gray    -- single-channel uint8 image containing the box
    contour -- the box's external contour
    thresh  -- the THRESH_BINARY_INV+OTSU threshold of `gray` (box == 255)
    lo, hi  -- inclusive validity range; a read outside it is rejected

    value is None when unreadable OR out of range. Callers must treat both
    the same way: do not trust the read.
    """
    target_h = int(os.environ.get("LEDGER_DIGIT_TARGET_H", "96"))
    pad_px = int(os.environ.get("LEDGER_DIGIT_PAD_PX", "25"))
    erode_cap = int(os.environ.get("LEDGER_DIGIT_ERODE_PX", "9"))
    psm_list = [p.strip() for p in
                os.environ.get("LEDGER_DIGIT_PSM_LIST", "7,8,6").split(",")
                if p.strip()]

    _, _, w, h = cv2.boundingRect(contour)
    erode_px = max(2, min(erode_cap, h // 8, w // 8))

    mask = np.zeros_like(thresh)
    # Fill the CONVEX HULL, not the raw contour: a digit glyph wider than
    # its box (Chemistry Final p.4) punctures the rim; the hull bridges the
    # breach. For an intact box, hull == contour.
    cv2.drawContours(mask, [cv2.convexHull(contour)], -1, 255, -1)
    mask = cv2.erode(mask, np.ones((erode_px, erode_px), np.uint8))
    if not mask.any():
        return None, ""

    # Digits stay grayscale: invert (light digits -> dark ink on light
    # field), blank everything outside the eroded mask to paper white.
    inv = 255 - gray
    inv[mask == 0] = 255
    ys, xs = np.where(mask > 0)
    inv = inv[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    inv = cv2.normalize(inv, None, 0, 255, cv2.NORM_MINMAX)

    # Upscale the GRAYSCALE, then binarize at comfortable size. Binarizing
    # a ~2 px serif stroke at native resolution shreds it (the v2 defect).
    scale = target_h / float(inv.shape[0])
    big = cv2.resize(inv, None, fx=scale, fy=scale,
                     interpolation=cv2.INTER_CUBIC)
    big = cv2.GaussianBlur(big, (3, 3), 0)
    _, bw = cv2.threshold(big, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    bw = 255 - cv2.morphologyEx(255 - bw, cv2.MORPH_CLOSE,
                                np.ones((3, 3), np.uint8))

    # Speck filter by component area (>= 2% of the largest component or
    # 9 px). Replaces the destructive 2x2 MORPH_OPEN.
    n, lab, stats, _ = cv2.connectedComponentsWithStats(255 - bw)
    if n <= 1:
        return None, ""
    areas = stats[1:, cv2.CC_STAT_AREA]
    min_area = max(9, int(0.02 * areas.max()))
    clean = np.full_like(bw, 255)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= min_area:
            clean[lab == i] = 0
    bw = clean
    ys, xs = np.where(bw == 0)
    if ys.size == 0:
        return None, ""
    bw = bw[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    bw = cv2.copyMakeBorder(bw, pad_px, pad_px, pad_px, pad_px,
                            cv2.BORDER_CONSTANT, value=255)

    # Multi-PSM vote. PSM 8 alone chronically drops a digit from '11'/'16';
    # line modes (7, 6) recover it. Winner = highest per-word confidence;
    # agreement count breaks ties. Out-of-range reads never receive a vote.
    votes = {}
    raw_seen = []
    for psm in psm_list:
        data = pytesseract.image_to_data(
            bw, config="--psm {0} {1}".format(psm, _BOXED_DIGIT_WL),
            output_type=pytesseract.Output.DICT)
        txt = "".join(t for t in data["text"] if t.strip())
        confs = [float(c) for c, t in zip(data["conf"], data["text"])
                 if t.strip()]
        raw_seen.append(txt if txt else "-")
        if not txt.isdigit():
            continue
        val = int(txt)
        if lo is not None and val < lo:
            continue
        if hi is not None and val > hi:
            continue
        votes.setdefault(val, []).append(min(confs) if confs else 0.0)

    if not votes:
        return None, "/".join(raw_seen)
    best = max(votes.items(), key=lambda kv: (max(kv[1]), len(kv[1])))
    return best[0], str(best[0])
# --- end [PATCH:boxed-digit-ocr] -------------------------------------------
import getpass

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"
IS_LOCAL_DESKTOP = IS_WINDOWS or IS_MACOS

if IS_LOCAL_DESKTOP:
    from ledger_tools import configure_tesseract, xelatex_command
    if IS_MACOS:
        from ledger_local import LocalRuntime as DesktopRuntime, workspace_path
    else:
        from ledger_windows import WindowsRuntime as DesktopRuntime, workspace_path
else:
    import grp

# Identify the active user's home directory for data siloing
HOME_DIR = os.path.expanduser("~")

# Dynamically bind the Engine directory to the script's actual location in the unified vault
ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))

def _read_cv_image(path):
    if os.name != "nt":
        return cv2.imread(path)
    try:
        data = np.fromfile(path, dtype=np.uint8)
    except OSError:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None


def _write_cv_image(path, image):
    if os.name != "nt":
        return cv2.imwrite(path, image)
    encoded, data = cv2.imencode(os.path.splitext(path)[1], image)
    if not encoded:
        return False
    try:
        data.tofile(path)
    except OSError:
        return False
    return True


def _windows_report_name(name):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).rstrip(" .") or "Unknown"


# The Teacher's Siloed Workspace
LEDGER_DIR = str(workspace_path()) if IS_LOCAL_DESKTOP else os.path.join(HOME_DIR, "Ledger Workspace")

# Siloed App Data (Hidden with a '.' to keep the teacher's visible workspace clean)
TEMP_DIR = os.path.join(LEDGER_DIR, ".Temp-Ledger-Files")
DIAGNOSTIC_DIR = os.path.join(LEDGER_DIR, ".Diagnostic-Log")
LOG_FILE = os.path.join(DIAGNOSTIC_DIR, "ledger-diagnostics.log")
DB_PATH = os.path.join(LEDGER_DIR, ".ledger-database.db")

# Shared Global Assets (Read strictly from the unified vault)
WATERMARK_FILE = os.path.join(ENGINE_DIR, "Ledger-Watermark.png") 

# Visible Workspace Directories
MASTER_DIR = os.path.join(LEDGER_DIR, "01 Master Assignments")
UNEVALUATED_DIR = os.path.join(LEDGER_DIR, "02 Inbox Scans")
COURSES_DIR = os.path.join(LEDGER_DIR, "03 Classes")
SIS_EXPORTS_DIR = os.path.join(LEDGER_DIR, "04 SIS Exports")
ORIGINAL_SCANS_DIR = os.path.join(LEDGER_DIR, "05 Archive Originals")

PERIODS = [
    "1st Period", "2nd Period", "3rd Period", "4th Period", 
    "5th Period", "6th Period", "7th Period", "Unknown-Student"
]

# [PATCH:no-default-prompt] WO3: no grading prompt ships with the Ledger (like
# the web app). The teacher types or pastes the grading instructions into the
# GRADING INSTRUCTIONS box (EDIT LEDGER PROMPT); they live in the Settings DB
# under 'grading_instructions'. RUN refuses to grade while they are empty.
NO_INSTRUCTIONS_BANNER = "NO GRADING INSTRUCTIONS"                            # red status line
NO_INSTRUCTIONS_LOG = "NO GRADING INSTRUCTIONS — enter them before running"  # log line

# --- [PATCH:wizard-devapi] (WO-A7) ------------------------------------------
# Display-free pieces of the SYSTEM INITIALIZATION wizard: the provider
# completeness rule, the on-screen copy, and the measured window geometry.
# Kept at module level so tests/ can import and drive them without a display.
# [PATCH:aistudio-prefix-aq] WO2 D3: AI Studio issues "AQ." keys as well as
# the older "AIza". Prefix sanity stays warn-only, so a future format can
# never be refused -- it only draws the warning.
AI_STUDIO_PREFIXES = ("AQ.", "AIza")
ANTHROPIC_PREFIX = "sk-ant-"
DEFAULT_GUI_MODEL = "gemini-3.5-pro-preview"
CODEX_HANDOFF_PROMPT = "$ledger-grading Process Ledger jobs in this workspace while I run an evaluation batch."
DESKTOP_OCR_TIMEOUT = 330
DESKTOP_MODEL_PREFIXES = ("codex/", "copilot/")
COPILOT_BRIDGE_URL = "http://127.0.0.1:8790"
COPILOT_BRIDGE_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}")
AZURE_ENDPOINT = re.compile(
    r"https://[a-z0-9][a-z0-9-]*\.(?:openai\.azure\.com|services\.ai\.azure\.com)/?",
    re.IGNORECASE | re.ASCII,
)
AZURE_TENANT = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.IGNORECASE)
WIZARD_INSTITUTION_VALUE = "owner-operator"   # fixed; no school name is collected
# Measured on the live display (1360x768, work area 735) during WO-A7 -- see
# the WO-A7 report for the raw winfo_req* numbers. Largest state + margin.
# Wizard body content requests 700x632 (Advanced collapsed) / 719x838
# (expanded) + 16 px scrollbar + 8 px padding -> largest state 743x846.
WIZARD_GEOMETRY = (800, 850)
# Disclosure Toplevel requests 920x528 with its text box sized to its 21
# rendered lines (see open_disclosure): 920x560 = request + 32 px margin.
DISCLOSURE_GEOMETRY = (920, 560)
WIZARD_SCREEN_ALLOWANCE = 100       # same screen clamp the dashboard uses
WIZARD_LEVEL_COLORS = {"block": "#c0392b", "warn": "#e67e22", "ready": "#1e8449", "idle": "#7f8c8d"}
WIZARD_DEFAULT_RULE = (
    "Provide GOOGLE (AI Studio API key, or Project ID + service-account JSON under "
    "Advanced), ANTHROPIC (API key), Azure OpenAI, Codex desktop, or a Copilot bridge token. "
    "At least one complete provider is required."
)
WIZARD_BILLING_REQUIRED = "Billing confirmation required for Google AI Studio keys."

# [PATCH:evaluation-wording] Evaluation-only terms (see DISCLAIMER.md).
WIZARD_LEGAL_TEXT = (
    "EXPERIMENTAL SOFTWARE -- EVALUATION TERMS AND ASSUMPTION OF RISK\n\n"

    "The Ledger is experimental software published for evaluation by school and "
    "district IT professionals. It is not a finished product, it is not certified "
    "or approved for use with student data, and it is not intended for use by "
    "individual teachers. Its author and Paragon Electrical Contracting, LLC "
    "(d/b/a Paragon, LLC and ParagonK12, LLC) do not recommend or encourage its "
    "use by anyone. By entering provider credentials and initializing this "
    "machine, I, the operator named above, agree to the following:\n\n"

    "    1. APPROVAL AND COMPLIANCE: I am solely responsible for obtaining every "
    "approval my school, district, or institution requires before this software "
    "is installed or used, and for compliance with every applicable law, "
    "regulation, contract, and policy, including FERPA, state student-privacy "
    "law, and any data-privacy agreement my institution requires.\n\n"

    "    2. DATA SENT TO THIRD PARTIES: This software sends images to the AI "
    "providers I configure (Google, Anthropic, Azure OpenAI, Codex, or Microsoft Copilot): the blank master pages, "
    "a title crop, an ID crop from every page, and either cropped answer boxes "
    "(WORKSHEET mode) or WHOLE PAGES (ESSAY mode). What a provider does with "
    "those images is governed by its terms, which I have read and accept.\n\n"

    "    3. NOT HARDENED: This machine has no disk encryption, no hardware-key "
    "login, and no kiosk lockdown. Securing it is my responsibility.\n\n"

    "    4. AI OUTPUT CAN BE WRONG: Every score and comment must be reviewed by a "
    "licensed educator before it is used for any purpose.\n\n"

    "    5. COSTS: I am solely responsible for every charge incurred on the "
    "provider account(s) I enter.\n\n"

    "    6. NO WARRANTY, NO LIABILITY: This software is provided AS IS, without "
    "warranty of any kind, under the GNU Affero General Public License v3.0 and "
    "the additional terms in DISCLAIMER.md. To the fullest extent permitted by "
    "law, the author and Paragon Electrical Contracting, LLC (d/b/a Paragon, LLC "
    "and ParagonK12, LLC) shall not be liable for any claim, loss, or damage of "
    "any kind arising from this software or its use, including disclosure of "
    "data, inaccurate scores, regulatory penalties, employment consequences, or "
    "cloud-service charges. I use it entirely at my own risk."
)

# §4e body text. [PATCH:evaluation-wording] Deliberately does NOT describe or
# interpret Google's terms; it points the operator to them.
BILLING_DISCLOSURE_TEXT = (
    "This software sends images of scanned pages to Google's Gemini service: a "
    "crop of the ID box on every page, and either cropped answer boxes (WORKSHEET "
    "mode) or WHOLE PAGES (ESSAY mode). Anything written on a page, including a "
    "name, can be sent. Treat everything you test with as a student record.\n\n"

    "Google offers the Gemini API under separate terms for unpaid (free-tier) and "
    "paid use, and those terms treat submitted material differently. This software "
    "does not describe or interpret Google's terms, and they can change at any time. "
    "You and your institution must read them and decide whether they are acceptable "
    "before any real student work is processed.\n\n"

    "Which of Google's terms apply is set by your Google account and project, not "
    "by this software. Confirm the status of your own project with Google.\n\n"

    "BEFORE CONTINUING: open Google AI Studio, go to Settings > Plan, and enable "
    "billing on the project that owns this key. All charges appear on that account "
    "and are your responsibility. If you have not done this, do not proceed -- the "
    "setup is reversible, and the key can be entered after billing is on.\n\n"

    "By checking the box below you confirm that billing is enabled on the key you "
    "entered, that you have read Google's terms, and that you, not the author or "
    "Paragon, are responsible for deciding whether they are acceptable.\n\n"

    "Reference: https://ai.google.dev/gemini-api/terms  (sections \"Unpaid Services\" "
    "and \"Paid Services\")"
)


def wizard_provider_state(ai_key, gcp_id, has_creds, anth_key, billing_confirmed,
                          azure_endpoint="", azure_tenant="", codex_enabled=False, copilot_token="",
                          local_copilot=False):
    """Pure provider-completeness rule for the SYSTEM INITIALIZATION wizard.

    Google is complete as EITHER an AI Studio key OR (Project ID + service-account
    JSON), Anthropic as an sk-ant- key, Azure as an endpoint plus tenant ID,
    Codex as an enabled desktop handoff, and Copilot via a local or manually configured bridge.
    At least one complete provider is required.
    Both Google forms at once -> block (one Google route per box). A half-entered
    Vertex pair -> block. A malformed Anthropic key -> block. An AI Studio key
    without the billing attestation -> block. Prefix sanity (AQ./AIza / sk-ant- in the
    wrong field) -> warn only.

    Returns a dict: ok (bool), level ('block'|'warn'|'ready'|'idle'), message
    (status-line text), warnings (list), and the selected provider flags.
    """
    ai_key = (ai_key or "").strip()
    gcp_id = (gcp_id or "").strip()
    anth_key = (anth_key or "").strip()
    azure_endpoint = (azure_endpoint or "").strip()
    azure_tenant = (azure_tenant or "").strip()
    copilot_token = (copilot_token or "").strip()
    has_creds = bool(has_creds)

    ai_present = bool(ai_key)
    vertex_complete = bool(gcp_id) and has_creds
    vertex_partial = bool(gcp_id) != has_creds
    both_google = ai_present and (bool(gcp_id) or has_creds)
    anth_complete = anth_key.startswith(ANTHROPIC_PREFIX)
    anth_malformed = bool(anth_key) and not anth_complete
    azure_complete = bool(AZURE_ENDPOINT.fullmatch(azure_endpoint) and AZURE_TENANT.fullmatch(azure_tenant))
    copilot_complete = local_copilot or bool(COPILOT_BRIDGE_TOKEN.fullmatch(copilot_token))

    warnings = []
    if ai_present and ai_key.startswith(ANTHROPIC_PREFIX):
        warnings.append("The GOOGLE AI STUDIO field holds an Anthropic key (sk-ant-...). "
                        "Move it to the ANTHROPIC field.")
    elif ai_present and not ai_key.startswith(AI_STUDIO_PREFIXES):
        warnings.append("AI Studio keys normally begin with AQ. or AIza -- check the paste.")

    def _out(ok, level, message, use_ai=False, use_vertex=False, use_anth=False):
        return {"ok": ok, "level": level, "message": message, "warnings": warnings,
                "use_ai_studio": use_ai, "use_vertex": use_vertex, "use_anthropic": use_anth,
                "use_azure": ok and azure_complete, "use_codex": ok and codex_enabled,
                "use_copilot": ok and copilot_complete}

    if both_google:
        return _out(False, "block",
                    "Choose ONE Google route: an AI Studio key OR Project ID + "
                    "service-account JSON (Advanced), not both.")
    if vertex_partial:
        return _out(False, "block",
                    "Google Cloud (Advanced) is half-entered: Project ID and "
                    "service-account JSON are BOTH required. Complete the pair, clear "
                    "it, or use an AI Studio key instead.")
    if anth_malformed:
        if anth_key.startswith(AI_STUDIO_PREFIXES):
            return _out(False, "block",
                        "The ANTHROPIC field holds a Google AI Studio key (AQ... or AIza...). "
                        "Move it to the GOOGLE AI STUDIO field.")
        return _out(False, "block",
                    "ANTHROPIC key doesn't look right: it must begin with sk-ant- "
                    "(copy the whole key from the Anthropic Console).")
    if ai_present and not billing_confirmed:
        return _out(False, "block", WIZARD_BILLING_REQUIRED)
    if (azure_endpoint or azure_tenant) and not azure_complete:
        return _out(False, "block", "Enter both the Azure OpenAI HTTPS resource URL and the Entra tenant GUID.")

    if copilot_token and not copilot_complete:
        return _out(False, "block", "Use the 32 to 128 character token generated for the Copilot bridge.")

    google_complete = ai_present or vertex_complete
    if not (google_complete or anth_complete or azure_complete or codex_enabled or copilot_complete):
        return _out(False, "idle", WIZARD_DEFAULT_RULE)

    names = []
    if ai_present:
        names.append("GOOGLE (AI Studio)")
    if vertex_complete:
        names.append("GOOGLE (Vertex)")
    if anth_complete:
        names.append("ANTHROPIC (direct API)")
    if azure_complete:
        names.append("Azure OpenAI (Microsoft Entra)")
    if codex_enabled:
        names.append("Codex desktop")
    if copilot_complete:
        names.append("Copilot desktop")
    ready = ("Providers ready: " if len(names) > 1 else "Provider ready: ") + " + ".join(names) + "."
    if warnings:
        return _out(True, "warn", ready + " WARNING: " + warnings[0],
                    ai_present, vertex_complete, anth_complete)
    return _out(True, "ready", ready, ai_present, vertex_complete, anth_complete)
# --- end [PATCH:wizard-devapi] ----------------------------------------------

class DesktopOcrError(RuntimeError):
    """A desktop OCR failure that leaves the batch pending."""


class LedgerGUI:
    def __init__(self, root, runtime=None):
        self.root = root
        self.runtime = runtime
        self._xelatex = "xelatex"
        self.root.title("DASHBOARD - EXPERIMENTAL EVALUATION BUILD")
        # [PATCH:topbar-fit] 1100 px cannot seat the essay-mode control row
        # (RUN + MAX SCORE + MODE selector + SHUT DOWN) -- the SHUT DOWN
        # button was clipped clean off the right edge. Open at 1615x920 (the
        # verified-fit size), clamped to the physical screen so a smaller
        # district monitor still gets the largest window that fits.
        _sw = self.root.winfo_screenwidth()
        _sh = self.root.winfo_screenheight()
        self.root.geometry(f"{min(1615, _sw - 40)}x{min(920, _sh - 100)}")
        
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        
        self.is_running = False
        self._batch_pending = False
        self._batch_model = None
        self.calibrated_this_session = False   # [PATCH:calib-session-skip] set by SAVE in either studio
        self.master_pdf_path = None
        self.cached_silo2_content = None
        self.master_group_key = None
        self.master_display_title = None
        
        self.roster = {} if self.runtime is not None else self.load_roster()
        
        self.header_font = ctk.CTkFont(family="Ubuntu", size=24, weight="bold")
        self.main_font = ctk.CTkFont(family="Ubuntu", size=18)
        self.log_font = ctk.CTkFont(family="Courier", size=16)
        
        self.setup_ui()
        self.verify_directories()
        self.initialize_database()
        if self.runtime is not None:
            self.roster = self.load_roster()
        self.clean_temp_directory() 
        self.setup_logging()
        self.check_first_run()
        
    def _daemon_request(self, method, endpoint, **kwargs):
        if self.runtime is not None:
            return self.runtime.request(method, endpoint, **kwargs)
        return requests.request(method, "http://127.0.0.1:5000" + endpoint, **kwargs)

    def load_roster(self):
        """Loads the roster into memory directly from the SQLite Database."""
        roster = {}
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            # Fills the memory dictionary using the DB so the pipeline functions normally
            cursor.execute("SELECT Bartonian_ID, Last_Name, First_Name FROM Roster")
            for row in cursor.fetchall():
                roster[str(row[0])] = {"last": row[1], "first": row[2]}
            conn.close()
        except Exception as e:
            self.log_message(f"[!] DB Roster Load Error: {e}")
        return roster

    def setup_ui(self):
        self.root.grid_columnconfigure(1, weight=1)
        self.root.grid_rowconfigure(0, weight=1)

        self.sidebar = ctk.CTkFrame(self.root, width=280, corner_radius=0, fg_color="black")
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        
        logo_path = os.path.join(ENGINE_DIR, "Boot Menu Logo1.png")
        try:
            logo_img = ctk.CTkImage(light_image=Image.open(logo_path), dark_image=Image.open(logo_path), size=(220, 220))
            self.logo_label = ctk.CTkLabel(self.sidebar, image=logo_img, text="")
            self.logo_label.grid(row=0, column=0, padx=30, pady=(40, 20))
        except Exception:
            self.logo_label = ctk.CTkLabel(self.sidebar, text="[Logo Missing]", font=self.header_font)
            self.logo_label.grid(row=0, column=0, padx=30, pady=(40, 20))

        self.status_label = ctk.CTkLabel(self.sidebar, text="SYSTEM: IDLE", font=self.header_font, text_color="#1e8449", wraplength=240)
        self.status_label.grid(row=1, column=0, padx=20, pady=10)

        # NEW UI: Blank Master PDF Uploader [cite: 92, 95]
        self.master_btn = ctk.CTkButton(self.sidebar, text="LOAD MASTER PDF", font=self.main_font, fg_color="#333333", hover_color="#555555", text_color="white", command=self.load_master_pdf)
        self.master_btn.grid(row=2, column=0, padx=20, pady=10)
        
        self.master_status_label = ctk.CTkLabel(self.sidebar, text="MASTER: NOT LOADED", font=self.main_font, text_color="white")
        self.master_status_label.grid(row=3, column=0, padx=20, pady=5)

        # NEW: Gradebook Launcher Button
        self.gradebook_btn = ctk.CTkButton(self.sidebar, text="OPEN GRADEBOOK", font=self.main_font, fg_color="#333333", hover_color="#555555", command=self.open_gradebook)
        self.gradebook_btn.grid(row=4, column=0, padx=20, pady=(20, 10))
        
        # SURGERY: ROSTER MANAGER BUTTON
        self.roster_btn = ctk.CTkButton(
            self.sidebar, 
            text="MANAGE ROSTER", 
            font=self.main_font, 
            fg_color="#333333", 
            hover_color="#555555", 
            command=self.open_roster_manager
        )
        self.roster_btn.grid(row=5, column=0, padx=20, pady=(10, 10))

        # SURGERY: ASSIGNMENT MANAGER BUTTON
        self.assignment_btn = ctk.CTkButton(
            self.sidebar, 
            text="MANAGE ASSIGNMENTS", 
            font=self.main_font, 
            fg_color="#333333", 
            hover_color="#555555", 
            command=self.open_assignment_manager
        )
        self.assignment_btn.grid(row=6, column=0, padx=20, pady=(10, 10))

        # CALIBRATE SCANNER BUTTON
        self.calibrate_btn = ctk.CTkButton(
            self.sidebar, 
            text="CALIBRATE SCANNER", 
            font=self.main_font, 
            fg_color="#333333", 
            hover_color="#555555", 
            command=self.open_calibration_tool
        )
        self.calibrate_btn.grid(row=7, column=0, padx=20, pady=(10, 10))

        # ALL-USERS AI MODEL PICKER (per-user; persists in each user's own DB)
        self.model_btn = ctk.CTkButton(
            self.sidebar,
            text="AI MODEL",
            font=self.main_font,
            fg_color="#333333",
            hover_color="#555555",
            command=self.open_model_picker
        )
        self.model_btn.grid(row=9, column=0, padx=20, pady=(10, 10))

        self.edit_prompt_btn = ctk.CTkButton(
            self.sidebar,
            text="EDIT LEDGER PROMPT",
            font=self.main_font,
            fg_color="#333333",
            hover_color="#555555",
            command=self.open_prompt_editor
        )
        self.edit_prompt_btn.grid(row=8, column=0, padx=20, pady=(10, 10))

        try:
            if self.runtime is not None:
                is_admin = True
                appliance_tools = False
            else:
                current_user = getpass.getuser()
                admin_groups = ['faculty-admin', 'district-admin', 'sudo']
                user_groups = [g.gr_name for g in grp.getgrall() if current_user in g.gr_mem]
                is_admin = current_user == 'paragon' or any(group in user_groups for group in admin_groups)
                appliance_tools = (os.path.exists("/opt/ledger/engine/ledger-keymgr-gui.py")
                                   and os.path.exists("/usr/local/sbin/ledger-usermgr"))

            if is_admin:
                self.advanced_btn = ctk.CTkButton(
                    self.sidebar,
                    text="ADVANCED",
                    font=self.main_font,
                    fg_color="#333333",
                    hover_color="#555555",
                    command=self.open_advanced_settings
                )
                self.advanced_btn.grid(row=10, column=0, padx=20, pady=(10, 10))

            if appliance_tools and is_admin:
                self.manage_users_btn = ctk.CTkButton(
                    self.sidebar, text="MANAGE OS USERS", font=self.main_font,
                    fg_color="#333333", hover_color="#555555", command=self.open_keys_and_accounts
                )
                self.manage_users_btn.grid(row=11, column=0, padx=20, pady=(10, 10))
            elif appliance_tools:
                self.enroll_key_btn = ctk.CTkButton(
                    self.sidebar, text="REGISTER MY KEY", font=self.main_font,
                    fg_color="#333333", hover_color="#555555", command=self.open_keys_and_accounts
                )
                self.enroll_key_btn.grid(row=11, column=0, padx=20, pady=(10, 10))
        except Exception as e:
            self.log_message(f"[!] Role-Based Sidebar Error: {e}")

        # Spacer
        self.sidebar.grid_rowconfigure(12, weight=1)

        self.main_area = ctk.CTkFrame(self.root, fg_color="transparent")
        self.main_area.grid(row=0, column=1, sticky="nsew", padx=20, pady=20)
        self.main_area.grid_rowconfigure(2, weight=1)
        self.main_area.grid_columnconfigure(0, weight=1)

        self.control_frame = ctk.CTkFrame(self.main_area, fg_color="transparent")
        self.control_frame.grid(row=0, column=0, sticky="ew", pady=(0, 20))

        self.run_btn = ctk.CTkButton(self.control_frame, text="RUN", font=self.header_font, height=55, width=160, fg_color="#1e8449", hover_color="#27ae60", command=self.start_ledger)
        self.run_btn.pack(side="left", padx=(0, 30))

        # [PATCH:topbar-fit] SHUT DOWN is created and PACKED EARLY, anchored
        # to the RIGHT edge. Tk pack priority is call order: earlier-packed
        # widgets keep their space when the frame is squeezed, so on a window
        # too narrow for the whole row it is the MODE selector (packed last)
        # that clips -- never the safety-critical SHUT DOWN button.
        self.shutdown_btn = ctk.CTkButton(self.control_frame, text="SHUT DOWN", font=self.header_font, height=55, width=160, fg_color="#c0392b", hover_color="#e74c3c", command=self.shut_down)
        self.shutdown_btn.pack(side="right", padx=(10, 0))

        self.score_label = ctk.CTkLabel(self.control_frame, text="MAX SCORE:", font=self.header_font)
        self.score_label.pack(side="left", padx=(0, 10))

        self.max_score_entry = ctk.CTkEntry(self.control_frame, font=self.header_font, width=90, height=45)
        self.max_score_entry.pack(side="left", padx=(0, 30))

        # [PATCH:essay-mode] Teacher-facing pipeline selector. The choice is
        # persisted in the Settings DB the moment it is tapped, and is read
        # ONCE per run (in start_ledger) so it can never change mid-batch.
        self.mode_label = ctk.CTkLabel(self.control_frame, text="MODE:", font=self.header_font)
        self.mode_label.pack(side="left", padx=(0, 10))

        self.mode_selector = ctk.CTkSegmentedButton(
            self.control_frame,
            values=["WORKSHEET (BOXED)", "ESSAY (FULL-PAGE)"],
            font=self.main_font,
            height=45,
            command=self.on_mode_selected
        )
        self.mode_selector.set("ESSAY (FULL-PAGE)" if self.get_grading_mode() == "essay" else "WORKSHEET (BOXED)")
        self.mode_selector.pack(side="left", padx=(0, 10))
        # [PATCH:topbar-fit] shutdown_btn moved above (packed early, side="right").

        self.log_label = ctk.CTkLabel(self.main_area, text="DIAGNOSTIC LOG:", font=self.main_font)
        self.log_label.grid(row=1, column=0, sticky="nw", pady=(0, 5))

        self.log_text = ctk.CTkTextbox(self.main_area, font=self.log_font, text_color="#2ecc71", fg_color="#121212", wrap="word", corner_radius=10)
        self.log_text.grid(row=2, column=0, sticky="nsew")

        # SURGICAL INSERTION: Universal Right-Click Menu for the Diagnostic Log
        def custom_log_paste():
            try:
                # Forcefully grab the main OS clipboard contents
                clipboard_text = self.root.clipboard_get()
                
                # Purge any highlighted text before pasting
                if self.log_text.tag_ranges("sel"):
                    self.log_text.delete("sel.first", "sel.last")
                    
                # Insert clipboard at the current cursor position
                self.log_text.insert("insert", clipboard_text)
            except tk.TclError:
                pass # Fails gracefully if the clipboard is completely empty

        log_menu = tk.Menu(self.log_text, tearoff=False, bg="#2b2b2b", fg="white")
        log_menu.add_command(label="Copy", command=lambda: self.log_text.event_generate("<<Copy>>"))
        log_menu.add_command(label="Paste", command=custom_log_paste)
        log_menu.add_command(label="Select All", command=lambda: self.log_text.tag_add("sel", "1.0", "end"))
        
        self.log_text.bind("<Button-3>", lambda event: log_menu.tk_popup(event.x_root, event.y_root))

        # Robust, consistent mouse-only Copy/Paste for the streaming terminal.
        self._add_textbox_menu(self.log_text)

    def load_master_pdf(self):
        picker_options = {
            "initialdir": MASTER_DIR,
            "title": "SELECT BLANK MASTER PDF",
            "filetypes": [("PDF Files", "*.pdf")],
        }
        if self.runtime is not None:
            file_path = filedialog.askopenfilename(parent=self.root, **picker_options)
        else:
            try:
                result = subprocess.run(
                    ['zenity', '--file-selection', f'--filename={MASTER_DIR}/',
                     '--title=SELECT BLANK MASTER PDF', '--file-filter=PDF files | *.pdf'],
                    capture_output=True, text=True
                )
                file_path = result.stdout.strip()
            except FileNotFoundError:
                file_path = filedialog.askopenfilename(**picker_options)

        if file_path:
            self.master_pdf_path = file_path
            filename = os.path.basename(file_path)
            self.master_status_label.configure(text=f"MASTER: {filename}", text_color="#1e8449")
            self.log_message(f"Master Copy loaded: {filename}. Ready for batch verification.")

    def run_batch_extraction(self, target_pdf):
        """Extracts the title using dynamic coordinates, registers assignment, and ignites pipeline."""
        self.log_message("Extracting official assignment title using calibrated zones...")
        try:
            doc = fitz.open(target_pdf)
            page = doc.load_page(0)
            pix = page.get_pixmap(matrix=fitz.Matrix(4.16, 4.16), alpha=False)
            
            temp_master_img_path = os.path.join(TEMP_DIR, "temp_title_extract_source.png")
            pix.save(temp_master_img_path)
            doc.close()

            img = _read_cv_image(temp_master_img_path)
            
            # Execute array slice using dynamic database coordinates
            t_coords = self.get_crop_coordinates('crop_title')
            title_crop = img[t_coords[1] : t_coords[1]+t_coords[3], t_coords[0] : t_coords[0]+t_coords[2]]
            
            temp_crop_path = os.path.join(TEMP_DIR, "temp_master_title_crop.png")
            _write_cv_image(temp_crop_path, title_crop)

            base64_img = self.image_to_base64_png(temp_crop_path)

            prompt = (
                "Extract the assignment/lab title from this cropped header image. "
                "Return ONLY a valid, flat JSON object using EXACTLY this template: "
                '{"assignment_title": "Exact Title Here"}'
            )

            payload = {
                "model_name": self.get_silo2_model(),
                "prompt": prompt,
                "image_b64": base64_img
            }
            
            use_desktop = payload["model_name"].lower().startswith(DESKTOP_MODEL_PREFIXES)
            if not use_desktop:
                time.sleep(10) # Network buffer before establishing Silo 1 session
            timeout = DESKTOP_OCR_TIMEOUT if use_desktop else 60
            response = self._daemon_request("POST", "/api/silo1", json=payload, timeout=timeout)
            if not self.is_running:
                return
            
            if response.status_code != 200:
                raise Exception(f"Daemon HTTP {response.status_code}: {response.text}")
                
            data = response.json()
            
            if data.get("status") == "success":
                if use_desktop:
                    title_data = json.loads(data['text'])
                else:
                    raw = data["text"].strip().replace('```json', '').replace('```', '').strip()
                    title_data = json_repair.loads(raw)
                if use_desktop and (not isinstance(title_data, dict) or not isinstance(title_data.get("assignment_title"), str)
                                  or not title_data["assignment_title"].strip()):
                    raise DesktopOcrError("The desktop provider returned an invalid assignment title.")
            else:
                raise Exception(data.get("message", "Daemon returned an error."))

            raw_title = title_data.get("assignment_title", "Unknown Assignment")
            self.master_group_key, self.master_display_title = self.process_assignment_names(raw_title)
            self.master_original_title = raw_title
            
            self.log_message(f"-> Official Title Locked: {self.master_display_title}")
            
            # Register the assignment before processing the batch.
            self.register_assignment_in_db(self.master_group_key, self.master_display_title, self.global_max_score)

        except Exception as e:
            self.log_message(f"[!] Critical Error extracting Title/Igniting Engine: {e}")
            return

        self.process_pipeline()

    def initialize_database(self):
        """Ensures the core database tables exist for a fresh Paragon unit."""
        try:
            conn = sqlite3.connect(DB_PATH)
            
            # Enable foreign key support in SQLite (required for cascading deletes)
            conn.execute("PRAGMA foreign_keys = ON")
            cursor = conn.cursor()
            
            # 1. Core Appliance Tables
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS Roster (
                    Bartonian_ID INTEGER PRIMARY KEY,
                    First_Name TEXT,
                    Last_Name TEXT
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS Assignments (
                    Assignment_Key TEXT PRIMARY KEY,
                    Display_Title TEXT,
                    Max_Score REAL
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS Grades (
                    Bartonian_ID INTEGER,
                    Assignment_Key TEXT,
                    Score_Earned REAL,
                    PRIMARY KEY (Bartonian_ID, Assignment_Key),
                    FOREIGN KEY (Bartonian_ID) REFERENCES Roster(Bartonian_ID) ON DELETE CASCADE,
                    FOREIGN KEY (Assignment_Key) REFERENCES Assignments(Assignment_Key) ON DELETE CASCADE
                )
            ''')
            
            # 2. Hidden Settings Table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS Settings (
                    Setting_Key TEXT PRIMARY KEY,
                    Setting_Value TEXT
                )
            ''')
            
            # 3. Inject default demonym
            cursor.execute('''
                INSERT OR IGNORE INTO Settings (Setting_Key, Setting_Value)
                VALUES ('id_demonym', 'Bartonian')
            ''')
            
            # 4. [PATCH:no-temperature] No Silo 2 temperature default: models
            #    run at vendor defaults. A legacy 'silo2_temperature' row in an
            #    existing DB is simply never read.

            # 6. Inject default Scanner Calibration Coordinates (X, Y, W, H)
            cursor.execute('''
                INSERT OR IGNORE INTO Settings (Setting_Key, Setting_Value)
                VALUES ('crop_title', '259,214,741,148')
            ''')
            cursor.execute('''
                INSERT OR IGNORE INTO Settings (Setting_Key, Setting_Value)
                VALUES ('crop_id', '1647,164,691,181')
            ''')
            cursor.execute('''
                INSERT OR IGNORE INTO Settings (Setting_Key, Setting_Value)
                VALUES ('crop_footer', '1200,3093,132,159')
            ''')
            # [PATCH:calib-seed] Title/ID seeds = the operator's own Letter calibration
            # of the Ledger form. The old ID seed (2380,236,1050,250) began at 93% of a
            # Letter page and ran off its right edge; the old title seed reached 70% of
            # the width and would overlap a correctly placed ID box. A DB whose three
            # rows still equal the old seeds was never calibrated, so it gets the new
            # seeds; a DB with any saved calibration is left alone.
            cursor.execute("SELECT Setting_Key, Setting_Value FROM Settings WHERE Setting_Key IN ('crop_title', 'crop_id', 'crop_footer')")
            if dict(cursor.fetchall()) == {'crop_title': '192,207,1598,119', 'crop_id': '2380,236,1050,250', 'crop_footer': '1200,3093,132,159'}:
                cursor.execute("UPDATE Settings SET Setting_Value = '259,214,741,148' WHERE Setting_Key = 'crop_title'")
                cursor.execute("UPDATE Settings SET Setting_Value = '1647,164,691,181' WHERE Setting_Key = 'crop_id'")
            
            # 7. [PATCH:essay-mode] Inject default grading mode (boxed = the
            # original stitched-grid pipeline; essay = FULL_PAGE pipeline)
            cursor.execute('''
                INSERT OR IGNORE INTO Settings (Setting_Key, Setting_Value)
                VALUES ('grading_mode', 'boxed')
            ''')
            
            conn.commit()
            conn.close()
        except Exception as e:
            self.log_message(f"[!] Database Initialization Error: {e}")

    def _is_initializer(self):
        """Allow setup for the desktop user or a Debian appliance initializer."""
        if self.runtime is not None:
            return True
        try:
            current_user = getpass.getuser()
            init_groups = ['district-admin', 'sudo']
            user_groups = [g.gr_name for g in grp.getgrall() if current_user in g.gr_mem]
            return current_user == 'paragon' or any(gn in user_groups for gn in init_groups)
        except Exception:
            return False

    def check_first_run(self):
        """Use the initialized marker for this desktop account or Debian appliance."""
        marker = self.runtime.init_path if self.runtime is not None else '/etc/ledger-initialized'
        system_initialized = os.path.exists(marker)
        if system_initialized:
            return  # Already set up -> every tier just runs.
        if self._is_initializer():
            self.lock_system_for_setup()
        else:
            self.show_awaiting_setup_notice()

    def show_awaiting_setup_notice(self):
        """Non-administrator on an un-initialized appliance: NO wizard, just a
        plain notice and a disabled RUN button."""
        try:
            self.run_btn.configure(text="LOCKED", fg_color="#c0392b",
                                   hover_color="#c0392b", state="disabled")
            self.status_label.configure(text="AWAITING SETUP", text_color="#c0392b")
            notice = ctk.CTkToplevel(self.root)
            notice.title("SYSTEM NOT YET INITIALIZED")
            notice.geometry("560x270")
            notice.transient(self.root)
            ctk.CTkLabel(notice, text="SYSTEM NOT YET INITIALIZED",
                         font=self.header_font).pack(pady=(30, 15))
            ctk.CTkLabel(
                notice,
                text=("This Ledger has not been set up yet.\n\n"
                      "First-time setup runs from the owner (paragon) account.\n"
                      "Evaluation is unavailable until then."),
                font=self.main_font, justify="center"
            ).pack(pady=10, padx=20)
            ctk.CTkButton(notice, text="OK", font=self.main_font, width=120,
                          command=notice.destroy).pack(pady=20)
            self._make_text_copyable(notice)
            notice.after(200, notice.grab_set)
        except Exception as e:
            self.log_message(f"[!] Awaiting-setup notice error: {e}")

    def lock_system_for_setup(self):
        self.run_btn.configure(text="AUTHORIZE", fg_color="#c0392b", hover_color="#e74c3c", command=self.launch_setup_gui)
        self.status_label.configure(text="SYSTEM: LOCKED", text_color="#c0392b")
        self.root.after(500, self.launch_setup_gui)

    def launch_setup_gui(self):
        """First-time setup for the owner-operator: provider credentials,
        identity, and the liability terms. [PATCH:wizard-anthropic] added the
        Anthropic route. [PATCH:wizard-devapi] (WO-A7) makes a Google AI Studio
        key the PRIMARY Google option, demotes the Vertex inputs (Project ID +
        service-account JSON + root CA) to a collapsed Advanced section, drops
        the institution field (a single operator initializes this box), and
        adds the blocking billing-disclosure screen for AI Studio keys. The
        completeness rule lives in wizard_provider_state() (module level,
        display-free) so tests/ can drive it."""
        import tkinter.font as tkfont
        setup_win = ctk.CTkToplevel(self.root)
        setup_win.title("SYSTEM INITIALIZATION")
        # [PATCH:wizard-devapi] Geometry measured on the live display during
        # WO-A7 (see WIZARD_GEOMETRY). Clamped to the physical screen the same
        # way the dashboard is; the body is a scrollable frame, so a clamped
        # window scrolls instead of clipping the AUTHORIZE button.
        _sw, _sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        _w = min(WIZARD_GEOMETRY[0], _sw - 40)
        _h = min(WIZARD_GEOMETRY[1], _sh - WIZARD_SCREEN_ALLOWANCE)
        setup_win.geometry(f"{_w}x{_h}")
        setup_win.minsize(_w, _h)
        setup_win.transient(self.root)

        body = ctk.CTkScrollableFrame(setup_win, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=4, pady=4)
        small_font = ctk.CTkFont(family="Ubuntu", size=11)
        note_font = ctk.CTkFont(family="Ubuntu", size=12)

        header = ctk.CTkLabel(body, text="EVALUATION TERMS AND ASSUMPTION OF RISK", font=self.header_font)
        header.pack(pady=(16, 12))

        # Measurement / test seam (WO-A7 geometry check). Never read at runtime.
        hooks = {}
        self._wizard_hooks = hooks

        def _paste_into(entry):
            try:
                entry.insert("insert", setup_win.clipboard_get())
            except tk.TclError:
                pass

        def _masked_row(parent, label):
            """One masked credential entry with a SHOW toggle and a right-click
            Paste menu (the key IS the credential; SHOW lets the operator verify
            the paste). Returns (entry, StringVar)."""
            frame = ctk.CTkFrame(parent, fg_color="transparent")
            frame.pack(pady=5)
            ctk.CTkLabel(frame, text=label, font=self.main_font).pack(side="left", padx=10)
            var = ctk.StringVar(value="")
            entry = ctk.CTkEntry(frame, font=self.main_font, width=350, show="•", textvariable=var)
            entry.pack(side="left")
            show_var = ctk.StringVar(value="off")
            ctk.CTkCheckBox(frame, text="SHOW", variable=show_var, onvalue="on", offvalue="off",
                            width=60, font=small_font,
                            command=lambda: entry.configure(
                                show="" if show_var.get() == "on" else "•")).pack(side="left", padx=8)
            menu = tk.Menu(entry, tearoff=False, bg="#2b2b2b", fg="white")
            menu.add_command(label="Paste", command=lambda: _paste_into(entry))
            entry.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))
            return entry, var

        # --- GOOGLE AI STUDIO KEY (primary Google option) ---
        ai_entry, ai_var = _masked_row(body, "GOOGLE AI STUDIO API KEY:")
        # --- ANTHROPIC KEY ([PATCH:wizard-anthropic]) ---
        anth_entry, anth_var = _masked_row(body, "ANTHROPIC API KEY:")

        azure_frame = ctk.CTkFrame(body, fg_color="transparent")
        azure_frame.pack(pady=5)
        azure_endpoint_var = ctk.StringVar(value="")
        azure_tenant_var = ctk.StringVar(value="")
        for row, label, variable in (
            (0, "Azure OpenAI resource URL", azure_endpoint_var),
            (1, "Microsoft Entra tenant ID", azure_tenant_var),
        ):
            ctk.CTkLabel(azure_frame, text=label, font=self.main_font).grid(row=row, column=0, sticky="w", padx=10)
            ctk.CTkEntry(azure_frame, font=self.main_font, width=350, textvariable=variable).grid(
                row=row, column=1, pady=4)
        azure_note = ("For Azure, sign in with Azure CLI under the account running Ledger."
                      if self.runtime is not None else
                      "For Azure, sign in with Azure CLI as the paragon user.")
        ctk.CTkLabel(azure_frame, text=azure_note, font=note_font).grid(
            row=2, column=0, columnspan=2, pady=(0, 4))

        codex_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(body, text="Use Codex desktop for evaluation", variable=codex_var,
                        font=self.main_font).pack(pady=(4, 2))
        ctk.CTkLabel(body, text="Open the handoff folder in Codex and use synthetic work for evaluation.",
                     font=note_font).pack(pady=(0, 8))

        local_copilot_account = os.environ.get("LEDGER_COPILOT_LOCAL_ACCOUNT", "").strip() if IS_WINDOWS else ""
        if local_copilot_account:
            copilot_token_var = ctk.StringVar(value="")
            copilot_note = "Copilot on this PC is selected. Ledger will manage the bridge credential."
        else:
            _, copilot_token_var = _masked_row(body, "COPILOT BRIDGE TOKEN:")
            copilot_note = ("For Copilot, start the bridge on this computer before grading." if IS_WINDOWS
                            else "For Copilot, start the Windows bridge and SSH tunnel before grading.")
        ctk.CTkLabel(body, text=copilot_note, font=note_font).pack(pady=(0, 8))

        # One-line provider rule so the operator knows what unlocks the button.
        provider_rule = ctk.CTkLabel(body, text=WIZARD_DEFAULT_RULE, font=note_font,
                                     text_color="#7f8c8d", wraplength=700, justify="center")
        provider_rule.pack(pady=(0, 4))

        disclosure_btn = ctk.CTkButton(
            body, text="BILLING DISCLOSURE (REQUIRED FOR AI STUDIO KEYS)", font=self.main_font,
            fg_color="#1f538d", hover_color="#14375e", state="disabled",
            command=lambda: open_disclosure())
        disclosure_btn.pack(pady=(4, 8))

        # --- ADVANCED: institutional Google Cloud (Vertex) inputs, collapsed ---
        # Functionality unchanged from the district build; only demoted.
        adv_state = {"open": False}
        _ADV_CLOSED = "▸  ADVANCED — INSTITUTIONAL GOOGLE CLOUD ACCOUNTS"
        _ADV_OPEN = "▾  ADVANCED — INSTITUTIONAL GOOGLE CLOUD ACCOUNTS"
        adv_btn = ctk.CTkButton(body, text=_ADV_CLOSED, font=self.main_font, width=600,
                                fg_color="#333333", hover_color="#555555",
                                command=lambda: toggle_advanced())
        adv_btn.pack(pady=(4, 4))
        adv_frame = ctk.CTkFrame(body, fg_color="#1e1e1e")
        ctk.CTkLabel(
            adv_frame,
            text=("Only for an institution-managed Google Cloud project (Vertex AI). Use either "
                  "this or the AI Studio key above -- never both."),
            font=note_font, text_color="#7f8c8d", wraplength=660, justify="center"
        ).pack(pady=(10, 4), padx=10)

        gcp_frame = ctk.CTkFrame(adv_frame, fg_color="transparent")
        gcp_frame.pack(pady=5)
        ctk.CTkLabel(gcp_frame, text="GOOGLE CLOUD PROJECT ID:", font=self.main_font).pack(side="left", padx=10)
        gcp_var = ctk.StringVar(value="")
        key_entry = ctk.CTkEntry(gcp_frame, font=self.main_font, width=350, textvariable=gcp_var)
        key_entry.pack(side="left")
        entry_menu = tk.Menu(key_entry, tearoff=False, bg="#2b2b2b", fg="white")
        entry_menu.add_command(label="Copy", command=lambda: key_entry.event_generate("<<Copy>>"))
        entry_menu.add_command(label="Paste", command=lambda: _paste_into(key_entry))
        key_entry.bind("<Button-3>", lambda e: entry_menu.tk_popup(e.x_root, e.y_root))

        # A Project ID alone does NOT authenticate to Vertex. A service-account
        # JSON key is the browser-free credential; it is staged in the per-user
        # temp dir and moved into /etc/paragon by save_and_boot in the SAME
        # privileged step (one password prompt).
        creds_state = {'path': None}

        def import_google_creds():
            try:
                if self.runtime is not None:
                    picked = filedialog.askopenfilename(
                        parent=setup_win, title="SELECT GOOGLE SERVICE-ACCOUNT KEY (.json)",
                        filetypes=[("Google credentials", "*.json")],
                    )
                else:
                    # Release the Tk grab while the external GTK picker is open.
                    setup_win.grab_release()
                    try:
                        result = subprocess.run(
                            ['zenity', '--file-selection',
                             '--title=SELECT GOOGLE SERVICE-ACCOUNT KEY (.json)',
                             '--file-filter=Google credentials | *.json'],
                            capture_output=True, text=True
                        )
                        picked = result.stdout.strip()
                    finally:
                        setup_win.grab_set()
                if picked:
                    creds_state['path'] = picked
                    creds_status.configure(text=f"Loaded: {os.path.basename(picked)}",
                                           text_color="#1e8449")
                    self.log_message(f"Google credentials selected: {os.path.basename(picked)}")
                    validate_inputs()
            except Exception as e:
                self.log_message(f"[!] Credential selection error: {e}")

        creds_btn = ctk.CTkButton(adv_frame, text="IMPORT SERVICE-ACCOUNT JSON (.json)",
                                  font=self.main_font, fg_color="#1f538d",
                                  hover_color="#14375e", command=import_google_creds)
        creds_btn.pack(pady=(5, 5))
        creds_status = ctk.CTkLabel(adv_frame, text="No service-account JSON loaded",
                                    font=self.main_font, text_color="#c0392b")
        creds_status.pack(pady=(0, 6))

        # --- ROOT CA INJECTION (only matters behind a TLS-intercepting proxy) ---
        def inject_ssl_cert():
            try:
                setup_win.grab_release()
                try:
                    result = subprocess.run(
                        ['zenity', '--file-selection', '--title=SELECT ROOT CA CERTIFICATE (.crt/.pem)', '--file-filter=Certificates | *.crt *.pem'],
                        capture_output=True, text=True
                    )
                finally:
                    setup_win.grab_set()
                cert_path = result.stdout.strip()
                if cert_path:
                    self.log_message(f"Installing root CA certificate: {os.path.basename(cert_path)}")

                    # Release UI lock so the OS pkexec password prompt doesn't freeze under the window
                    setup_win.grab_release()

                    cert_name = os.path.basename(cert_path)
                    # Debian/Ubuntu systems require a .crt extension to process the certificate
                    if not cert_name.endswith('.crt'):
                        cert_name += '.crt'

                    cmd = f"cp '{cert_path}' '/usr/local/share/ca-certificates/{cert_name}' && update-ca-certificates"
                    subprocess.run(['pkexec', 'sh', '-c', cmd], check=True)

                    self.log_message("-> SSL Certificate installed successfully.")
                    setup_win.grab_set() # Re-engage UI lock
            except subprocess.CalledProcessError:
                self.log_message("[!] ERROR: Password prompt failed or was cancelled. Certificate not installed.")
                setup_win.grab_set()
            except Exception as e:
                self.log_message(f"[!] SSL Injection Error: {e}")
                setup_win.grab_set()

        if self.runtime is None:
            ssl_btn = ctk.CTkButton(adv_frame, text="INSTALL ROOT CA CERT (OPTIONAL)", font=self.main_font,
                                    fg_color="#1f538d", hover_color="#14375e", command=inject_ssl_cert)
            ssl_btn.pack(pady=(4, 12))

        def toggle_advanced():
            adv_state["open"] = not adv_state["open"]
            if adv_state["open"]:
                adv_frame.pack(after=adv_btn, pady=(0, 8), padx=20, fill="x")
                adv_btn.configure(text=_ADV_OPEN)
            else:
                adv_frame.pack_forget()
                adv_btn.configure(text=_ADV_CLOSED)
        hooks["toggle_advanced"] = toggle_advanced

        # --- IDENTITY ---
        info_frame = ctk.CTkFrame(body, fg_color="transparent")
        info_frame.pack(pady=10)
        ctk.CTkLabel(info_frame, text="OPERATOR NAME:", font=self.main_font).grid(row=0, column=0, sticky="e", padx=10, pady=5)
        rep_var = ctk.StringVar(value="")
        rep_entry = ctk.CTkEntry(info_frame, font=self.main_font, width=300, textvariable=rep_var)
        rep_entry.grid(row=0, column=1, sticky="w", pady=5)

        # --- LEGAL TEXT BOX ---
        legal_box = ctk.CTkTextbox(body, width=700, height=200, wrap="word", font=ctk.CTkFont(family="Ubuntu", size=13))
        legal_box.pack(pady=8)
        legal_box.insert("0.0", WIZARD_LEGAL_TEXT)
        legal_box.configure(state="disabled") # Lock the text from being edited

        # --- THE LIABILITY MECHANISM ---
        check_var = ctk.StringVar(value="off")
        liability_cb = ctk.CTkCheckBox(
            body,
            text="I HAVE READ AND AGREE TO THESE TERMS. I UNDERSTAND THIS IS EXPERIMENTAL SOFTWARE FOR EVALUATION,\nPROVIDED AS IS WITH NO WARRANTY, AND I USE IT ENTIRELY AT MY OWN RISK.",
            variable=check_var, onvalue="on", offvalue="off",
            font=ctk.CTkFont(family="Ubuntu", size=11, weight="bold"),
            text_color="#c0392b"
        )
        liability_cb.pack(pady=(10, 14))

        submit_btn = ctk.CTkButton(
            body, text="Authorize Ledger" if self.runtime is not None else "AUTHORIZE APPLIANCE",
            font=self.header_font, height=50,
            fg_color="#333333", state="disabled"
        )
        submit_btn.pack(pady=(4, 14))

        # --- BILLING DISCLOSURE SCREEN ([PATCH:wizard-devapi] §4e) ---
        # Shown once an AI Studio key is entered (paste, or leaving the field).
        # The attestation is tied to the key text: editing the key clears it.
        billing_var = ctk.StringVar(value="off")
        disc = {"win": None, "shown_for": None, "confirmed_key": None}

        def _close_disclosure():
            w = disc["win"]
            disc["win"] = None
            if w is not None:
                try:
                    w.grab_release()
                    w.destroy()
                except tk.TclError:
                    pass
            try:
                setup_win.grab_set()
            except tk.TclError:
                pass
            validate_inputs()

        def _on_billing_toggle():
            disc["confirmed_key"] = ai_var.get().strip() if billing_var.get() == "on" else None
            validate_inputs()

        def open_disclosure():
            if disc["win"] is not None:
                try:
                    disc["win"].lift()
                    disc["win"].focus_force()
                    return
                except tk.TclError:
                    disc["win"] = None
            d = ctk.CTkToplevel(setup_win)
            disc["win"] = d
            d.title("Billing and Student Work — Required Reading")
            dw = min(DISCLOSURE_GEOMETRY[0], _sw - 40)
            dh = min(DISCLOSURE_GEOMETRY[1], _sh - WIZARD_SCREEN_ALLOWANCE)
            d.geometry(f"{dw}x{dh}")
            d.minsize(dw, dh)
            d.transient(setup_win)
            d.protocol("WM_DELETE_WINDOW", _close_disclosure)
            ctk.CTkLabel(d, text="BILLING AND STUDENT WORK — REQUIRED READING",
                         font=self.header_font).pack(pady=(18, 8))
            box = ctk.CTkTextbox(d, width=dw - 60, wrap="word",
                                 font=ctk.CTkFont(family="Ubuntu", size=13))
            box.pack(pady=(0, 6), padx=30)
            box.insert("0.0", BILLING_DISCLOSURE_TEXT)
            box.configure(state="disabled")
            # Size the box to its rendered lines so nothing scrolls off-panel.
            d.update_idletasks()
            d.update()
            try:
                # count() returns display-line BOUNDARIES, i.e. lines - 1.
                n_lines = box._textbox.count("1.0", "end-1c", "displaylines")[0] + 1
                line_h = tkfont.Font(font=box._textbox.cget("font")).metrics("linespace")
                box.configure(height=n_lines * line_h + 32)
            except Exception:
                box.configure(height=460)
            cb = ctk.CTkCheckBox(
                d, text="I have enabled billing on this Google AI Studio key.",
                variable=billing_var, onvalue="on", offvalue="off",
                font=ctk.CTkFont(family="Ubuntu", size=13, weight="bold"),
                text_color="#c0392b", command=_on_billing_toggle)
            cb.pack(pady=(8, 8))
            ctk.CTkButton(d, text="CONTINUE", font=self.header_font, height=44, width=220,
                          command=_close_disclosure).pack(pady=(0, 16))
            hooks["disclosure_win"] = d
            hooks["disclosure_textbox"] = box
            self._make_text_copyable(d)
            setup_win.grab_release()
            d.after(200, d.grab_set)
        hooks["open_disclosure"] = open_disclosure
        hooks.update(ai_var=ai_var, anth_var=anth_var, gcp_var=gcp_var, rep_var=rep_var,
                     creds_state=creds_state, billing_var=billing_var, check_var=check_var,
                     submit_btn=submit_btn, provider_rule=provider_rule, disc=disc,
                     on_billing_toggle=_on_billing_toggle, close_disclosure=_close_disclosure)

        ai_prev = {"len": 0}

        def _on_ai_change(*_):
            val = ai_var.get().strip()
            pasted = (len(val) - ai_prev["len"]) > 1
            ai_prev["len"] = len(val)
            if not val:
                disc["shown_for"] = None
            elif pasted and disc["shown_for"] != val:
                disc["shown_for"] = val
                setup_win.after(50, open_disclosure)
            validate_inputs()

        def _on_ai_focus_out(_e=None):
            val = ai_var.get().strip()
            if val and disc["shown_for"] != val and disc["win"] is None:
                disc["shown_for"] = val
                open_disclosure()

        # --- DYNAMIC VALIDATION LOGIC ---
        def validate_inputs(*args):
            """Identity + liability are always required. The provider rule is
            wizard_provider_state(): at least ONE complete provider, one Google
            route only, prefix sanity warnings, and a confirmed billing
            attestation whenever an AI Studio key is present."""
            ai_key = ai_var.get().strip()
            if billing_var.get() == "on" and ai_key != disc["confirmed_key"]:
                billing_var.set("off")
                disc["confirmed_key"] = None
            st = wizard_provider_state(ai_key, gcp_var.get(), creds_state['path'] is not None,
                                        anth_var.get(), billing_var.get() == "on",
                                        azure_endpoint_var.get(), azure_tenant_var.get(),
                                        codex_var.get(), copilot_token_var.get(), bool(local_copilot_account))
            disclosure_btn.configure(state="normal" if ai_key else "disabled")
            provider_rule.configure(text=st["message"], text_color=WIZARD_LEVEL_COLORS[st["level"]])
            has_rep = len(rep_var.get().strip()) > 0
            is_checked = check_var.get() == "on"
            if has_rep and is_checked and st["ok"]:
                submit_btn.configure(state="normal", fg_color="#1e8449", hover_color="#27ae60")
            else:
                submit_btn.configure(state="disabled", fg_color="#333333")

        ai_var.trace_add("write", _on_ai_change)
        ai_entry.bind("<FocusOut>", _on_ai_focus_out)
        for _v in (anth_var, gcp_var, rep_var, azure_endpoint_var, azure_tenant_var, codex_var, copilot_token_var):
            _v.trace_add("write", validate_inputs)
        liability_cb.configure(command=validate_inputs)

        # --- SECURE WRITE & BOOT EXECUTION ---
        def save_and_boot():
            # Provider-aware install. validate_inputs already guaranteed a
            # complete provider set; this writes only the lines/files that
            # belong to the providers given.
            ai_key = ai_var.get().strip()
            new_key = gcp_var.get().strip()
            anth_key = anth_var.get().strip()
            azure_endpoint = azure_endpoint_var.get().strip().rstrip("/")
            azure_tenant = azure_tenant_var.get().strip()
            copilot_token = copilot_token_var.get().strip()
            rep_name = rep_var.get().strip()
            timestamp = datetime.datetime.now().isoformat()

            st = wizard_provider_state(ai_key, new_key, creds_state['path'] is not None,
                                        anth_key, billing_var.get() == "on", azure_endpoint, azure_tenant,
                                        codex_var.get(), copilot_token, bool(local_copilot_account))
            use_ai = st["use_ai_studio"]
            use_vertex = st["use_vertex"]
            use_anthropic = st["use_anthropic"]
            use_azure = st["use_azure"]
            use_codex = st["use_codex"]
            use_copilot = st["use_copilot"]

            if st["ok"] and rep_name and check_var.get() == "on":
                env_lines = []
                if use_ai:
                    env_lines.append(f'LEDGER_GOOGLE_API_KEY="{ai_key}"\n')
                if use_vertex:
                    sa_path = self.runtime.sa_path if self.runtime is not None else "/etc/paragon/sa.json"
                    env_lines.extend((
                        f'GCP_PROJECT_ID="{new_key}"\n',
                        'GCP_LOCATION="global"\n',
                        f'GOOGLE_APPLICATION_CREDENTIALS="{sa_path}"\n',
                    ))
                if use_anthropic:
                    env_lines.append(f'LEDGER_ANTHROPIC_API_KEY="{anth_key}"\n')
                if use_azure:
                    env_lines.extend((
                        f'LEDGER_AZURE_OPENAI_ENDPOINT="{azure_endpoint}"\n',
                        f'LEDGER_AZURE_TENANT_ID="{azure_tenant}"\n',
                    ))
                if use_codex:
                    env_lines.append('LEDGER_CODEX_ENABLED="true"\n')
                if use_copilot and local_copilot_account:
                    cdp_port = os.environ.get("COPILOT_CDP_PORT", "9337").strip()
                    model = os.environ.get("LEDGER_DEFAULT_MODEL", "").strip()
                    if not re.fullmatch(r"[^\s\"']+@[^\s\"']+", local_copilot_account) or not cdp_port.isdecimal():
                        messagebox.showerror("Ledger setup", "Check the Copilot work account and debugging port.", parent=setup_win)
                        return
                    env_lines.extend((
                        f'LEDGER_COPILOT_LOCAL_ACCOUNT="{local_copilot_account}"\n',
                        f'LEDGER_COPILOT_CDP_PORT="{cdp_port}"\n',
                    ))
                    if re.fullmatch(r"copilot/[a-z0-9.-]+", model):
                        env_lines.append(f'LEDGER_DEFAULT_MODEL="{model}"\n')
                elif use_copilot:
                    env_lines.extend((
                        f'LEDGER_COPILOT_BRIDGE_URL="{COPILOT_BRIDGE_URL}"\n',
                        f'LEDGER_COPILOT_BRIDGE_TOKEN="{copilot_token}"\n',
                    ))
                env_text = "".join(env_lines)
                marker_text = f"initialized_by={rep_name}\ninitialized_at={timestamp}\n"

                if self.runtime is not None:
                    try:
                        self.runtime.save_setup(env_text, creds_state['path'] if use_vertex else None, marker_text)
                    except (OSError, RuntimeError) as exc:
                        messagebox.showerror("Ledger setup", str(exc), parent=setup_win)
                        return
                else:
                    # The appliance installs its settings through one privileged step.
                    temp_sa = os.path.join(TEMP_DIR, "sa.json.temp")
                    temp_marker = os.path.join(TEMP_DIR, "initialized.temp")
                    with tempfile.NamedTemporaryFile(mode="w", dir=TEMP_DIR, prefix=".env-", delete=False) as f:
                        temp_env = f.name
                        f.write(env_text)
                    if use_vertex:
                        try:
                            shutil.copy(creds_state['path'], temp_sa)
                        except Exception as e:
                            os.unlink(temp_env)
                            self.log_message(f"[!] ERROR: Could not read the credentials file: {e}")
                            return
                    with open(temp_marker, "w") as f:
                        f.write(marker_text)

                    # 2. ONE privileged step: create /etc/paragon, install .env (and
                    #    sa.json when the Vertex route is used) with daemon-readable
                    #    perms (root:paragon 0640), drop the world-readable
                    #    'initialized' marker, restart the daemon. A non-Vertex
                    #    install also REMOVES any stale sa.json so a re-initialized
                    #    box can't silently keep a prior Google Cloud identity.
                    self.log_message("Requesting your password to secure credentials...")

                    # Release the GUI input lock so the OS password prompt is usable
                    setup_win.grab_release()

                    try:
                        # Credential install is fatal-on-error (set -e); the daemon
                        # (re)start is best-effort -- it must NOT undo a good save if
                        # the broker isn't placed yet (system map register #1).
                        sa_step = (
                            f"mv '{temp_sa}' /etc/paragon/sa.json; "
                            "chown root:paragon /etc/paragon/sa.json; chmod 640 /etc/paragon/sa.json; "
                            if use_vertex else
                            "rm -f /etc/paragon/sa.json; "
                        )
                        combined_command = (
                            "set -e; "
                            "install -d -o root -g paragon -m 0750 /etc/paragon; "
                            f"mv '{temp_env}' /etc/paragon/.env; "
                            "chown root:paragon /etc/paragon/.env; chmod 640 /etc/paragon/.env; "
                            + sa_step +
                            f"mv '{temp_marker}' /etc/ledger-initialized; "
                            "chown root:root /etc/ledger-initialized; chmod 644 /etc/ledger-initialized; "
                            "set +e; systemctl restart ledger-daemon.service; exit 0"
                        )
                        subprocess.run(['pkexec', 'sh', '-c', combined_command], check=True)

                    except subprocess.CalledProcessError:
                        self.log_message("[!] ERROR: Password prompt failed or was cancelled. Keys not saved.")
                        # Re-engage the lock if the password prompt fails or is canceled
                        setup_win.grab_set()
                        return
                    finally:
                        if os.path.exists(temp_env):
                            os.unlink(temp_env)

                # 3. Record the authorization in the SQLite database. The
                #    liability_institution key is kept for schema compatibility
                #    (factory reset scrubs it) and holds a fixed value on this
                #    build -- no school name is collected.
                try:
                    conn = sqlite3.connect(DB_PATH)
                    cursor = conn.cursor()
                    cursor.executemany('''
                        INSERT OR REPLACE INTO Settings (Setting_Key, Setting_Value)
                        VALUES (?, ?)
                    ''', [
                        ('liability_signer', rep_name),
                        ('liability_institution', WIZARD_INSTITUTION_VALUE),
                        ('liability_timestamp', timestamp)
                    ])
                    conn.commit()
                    conn.close()
                except Exception as e:
                    self.log_message(f"[!] Database Write Error (Authorization Record): {e}")

                # 4. Finalizing Log
                providers = " + ".join(
                    p for p, on in (("GOOGLE (AI Studio)", use_ai), ("GOOGLE (Vertex)", use_vertex),
                                    ("ANTHROPIC", use_anthropic), ("Azure OpenAI", use_azure),
                                    ("Codex desktop", use_codex), ("Copilot desktop", use_copilot)) if on
                )
                if use_ai:
                    self.log_message("Google (AI Studio): billing confirmed by operator")
                self.log_message(f"-> AUTHORIZATION EXECUTED. Terms accepted by {rep_name}. Providers: {providers}.")

                # --- Unlock and restore the main GUI ---
                self.run_btn.configure(text="RUN", fg_color="#1e8449", hover_color="#27ae60", command=self.start_ledger)
                self.status_label.configure(text="SYSTEM: IDLE", text_color="#1e8449")

                # Release the hostage window
                _close_disclosure_quiet = disc["win"]
                if _close_disclosure_quiet is not None:
                    try:
                        _close_disclosure_quiet.destroy()
                    except tk.TclError:
                        pass
                setup_win.grab_release()
                setup_win.destroy()

        submit_btn.configure(command=save_and_boot)

        # Lock the window focus ONLY after all widgets have safely mapped to the screen
        self._make_text_copyable(setup_win)
        setup_win.grab_set()

    def get_custom_id_label(self):
        """Fetches the current pseudonym system label from the database."""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT Setting_Value FROM Settings WHERE Setting_Key = 'custom_id_label'")
            result = cursor.fetchone()
            conn.close()
            return result[0] if result else "Bartonian ID"
        except Exception as e:
            self.log_message(f"[!] Database Read Error (Pseudonym): {e}")
            return "Bartonian ID"

    def get_grading_mode(self):
        """[PATCH:essay-mode] Fetches the pipeline mode from the Settings DB.
        Returns 'boxed' (the original stitched-grid pipeline; default and
        fail-safe) or 'essay' (the FULL_PAGE pipeline). Any unexpected value
        collapses to 'boxed' so a corrupt row can never change chemistry."""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT Setting_Value FROM Settings WHERE Setting_Key = 'grading_mode'")
            result = cursor.fetchone()
            conn.close()
            return "essay" if (result and result[0] == "essay") else "boxed"
        except Exception as e:
            self.log_message(f"[!] Database Read Error (Grading Mode): {e}")
            return "boxed"

    def get_grading_instructions(self):
        """[PATCH:no-default-prompt] WO3: the teacher's grading instructions from
        the Settings DB. Nothing ships built in: no row (fresh install) or a
        read error returns '' and RUN refuses to grade."""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT Setting_Value FROM Settings WHERE Setting_Key = 'grading_instructions'")
            result = cursor.fetchone()
            conn.close()
            return result[0] if (result and result[0]) else ""
        except Exception as e:
            self.log_message(f"[!] Database Read Error (Grading Instructions): {e}")
            return ""

    def on_mode_selected(self, selected_value):
        """[PATCH:essay-mode] Persists the teacher's mode choice immediately."""
        mode = "essay" if "ESSAY" in str(selected_value).upper() else "boxed"
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO Settings (Setting_Key, Setting_Value)
                VALUES ('grading_mode', ?)
            ''', (mode,))
            conn.commit()
            conn.close()
            self.log_message(f"-> Grading mode set to: {'ESSAY (FULL-PAGE)' if mode == 'essay' else 'WORKSHEET (BOXED)'}.")
        except Exception as e:
            self.log_message(f"[!] Database Write Error (Grading Mode): {e}")

    def register_assignment_in_db(self, assignment_key, display_title, max_score):
        """Ensures the assignment exists in the DB before grades are written."""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR IGNORE INTO Assignments (Assignment_Key, Display_Title, Max_Score)
                VALUES (?, ?, ?)
            ''', (assignment_key, display_title, float(max_score)))
            conn.commit()
            conn.close()
        except Exception as e:
            self.log_message(f"[!] DB Assignment Registration Error: {e}")

    def write_grade_to_db(self, b_id, assignment_key, score_earned):
        """Writes or updates a student's grade for a specific assignment."""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            # INSERT OR REPLACE handles late work effortlessly
            cursor.execute('''
                INSERT OR REPLACE INTO Grades (Bartonian_ID, Assignment_Key, Score_Earned)
                VALUES (?, ?, ?)
            ''', (int(b_id), assignment_key, float(score_earned))) # SURGICAL INSERTION: Accept partial credit
            conn.commit()
            conn.close()
            self.log_message(f"  -> Score committed to SQLite Database.")
        except Exception as e:
            self.log_message(f"[!] Database Write Error for ID {b_id}: {e}")

    def open_system_settings(self):
        """Launches the System Settings interface for the Paragon appliance."""
        set_win = ctk.CTkToplevel(self.root)
        set_win.title("SYSTEM SETTINGS")
        set_win.geometry("500x300")
        set_win.transient(self.root) 
        
        current_demonym = self.get_demonym()

        header_label = ctk.CTkLabel(set_win, text="APPLIANCE SETTINGS", font=self.header_font)
        header_label.pack(pady=(30, 20))

        input_frame = ctk.CTkFrame(set_win, fg_color="transparent")
        input_frame.pack(pady=10)

        demo_label = ctk.CTkLabel(input_frame, text="Classroom Demonym (e.g., Bartonian):", font=self.main_font)
        demo_label.pack(side="left", padx=(0, 10))

        self.demo_entry = ctk.CTkEntry(input_frame, font=self.main_font, width=150)
        self.demo_entry.insert(0, current_demonym)
        self.demo_entry.pack(side="left")

        save_btn = ctk.CTkButton(
            set_win, 
            text="SAVE CONFIGURATION", 
            font=self.header_font,
            height=45,
            fg_color="#1e8449", # Green for safety/confirmation
            hover_color="#27ae60",
            command=lambda: self.save_settings(set_win)
        )
        save_btn.pack(pady=(30, 0))
        self._make_text_copyable(set_win)

    def save_settings(self, window):
        """Commits the updated settings to the SQLite database."""
        new_demonym = self.demo_entry.get().strip()
        if not new_demonym:
            new_demonym = "Bartonian" # Fallback if they leave it blank
            
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE Settings 
                SET Setting_Value = ? 
                WHERE Setting_Key = 'id_demonym'
            ''', (new_demonym,))
            conn.commit()
            conn.close()
            self.log_message(f"-> System Settings Updated: Demonym set to '{new_demonym}'.")
            window.destroy()
        except Exception as e:
            self.log_message(f"[!] Database Update Error (Settings): {e}")

    def get_silo2_model(self):
        """Return the batch model, or fetch the user preference when idle."""
        if self._batch_model is not None:
            return self._batch_model
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT Setting_Value FROM Settings WHERE Setting_Key = 'silo2_model'")
            result = cursor.fetchone()
            conn.close()
            if result and result[0] and result[0] != DEFAULT_GUI_MODEL:
                return result[0]
        except Exception as e:
            self.log_message(f"[!] Database Read Error (Model): {e}")
        try:
            health = self._daemon_request("GET", "/", timeout=1)
            health.raise_for_status()
            providers = health.json()
            return (providers.get("codex_default_model") or providers.get("copilot_default_model")
                    or providers.get("azure_openai_default_model")
                    or DEFAULT_GUI_MODEL)
        except (requests.RequestException, ValueError):
            return DEFAULT_GUI_MODEL

    def get_crop_coordinates(self, key):
        """Fetches a specific bounding box from the database as a tuple of ints (x, y, w, h)."""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT Setting_Value FROM Settings WHERE Setting_Key = ?", (key,))
            result = cursor.fetchone()
            conn.close()
            if result:
                return tuple(map(int, result[0].split(',')))
        except Exception as e:
            self.log_message(f"[!] DB Error reading {key}: {e}")
        # Failsafe defaults mirroring the initial DB creation
        defaults = {'crop_title': (259, 214, 741, 148), 'crop_id': (1647, 164, 691, 181), 'crop_footer': (1200, 3093, 132, 159)}
        return defaults.get(key, (0, 0, 100, 100))

    def set_user_model(self, model_name):
        """Persist this user's chosen model to their own siloed DB (UPSERT)."""
        model_name = (model_name or "").strip() or DEFAULT_GUI_MODEL
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO Settings (Setting_Key, Setting_Value) VALUES ('silo2_model', ?) "
                "ON CONFLICT(Setting_Key) DO UPDATE SET Setting_Value = excluded.Setting_Value",
                (model_name,)
            )
            conn.commit()
            conn.close()
            self.log_message(f"-> AI model set to '{model_name}' for this account.")
            return True
        except Exception as e:
            self.log_message(f"[!] Could not save model preference: {e}")
            return False

    def open_model_picker(self):
        """Choose an exact model ID and save it for this user."""
        win = ctk.CTkToplevel(self.root)
        win.title("AI MODEL")
        win.geometry("680x500")
        win.minsize(650, 480)
        win.resizable(True, True)
        win.transient(self.root)

        ctk.CTkLabel(win, text="CHOOSE YOUR AI MODEL", font=self.header_font).pack(pady=(24, 6))
        ctk.CTkLabel(
            win,
            text=("Enter the exact model ID.\n"
                  "Codex desktop: codex/gpt-6.1-sol or codex/gpt-6-astra\n"
                  "Copilot desktop: copilot/gpt-6.1-sol, copilot/gpt-6-astra, copilot/gpt-5.6-sol\n"
                  "Azure: gpt-6.1-sol or gpt-6-astra\n"
                  "Google: gemini-4-argon  |  Anthropic: claude-opus-4-8"),
            font=self.main_font, justify="center"
        ).pack(pady=(0, 14), padx=20)

        entry = ctk.CTkEntry(win, font=self.main_font, width=360)
        entry.insert(0, self.get_silo2_model())
        entry.pack(pady=(0, 16))

        # AI-MODEL-FIELD right-click copy/paste menu (mouse-driven; unique names avoid collisions).
        model_tkentry = getattr(entry, "_entry", entry)
        model_field_menu = tk.Menu(win, tearoff=False, bg="#2b2b2b", fg="white")
        model_field_menu.add_command(label="Cut", command=lambda: model_tkentry.event_generate("<<Cut>>"))
        model_field_menu.add_command(label="Copy", command=lambda: model_tkentry.event_generate("<<Copy>>"))
        model_field_menu.add_command(label="Paste", command=lambda: model_tkentry.event_generate("<<Paste>>"))
        model_field_menu.add_separator()

        def _model_field_select_all():
            try:
                model_tkentry.select_range(0, "end")
                model_tkentry.icursor("end")
            except tk.TclError:
                pass

        model_field_menu.add_command(label="Select All", command=_model_field_select_all)
        model_tkentry.bind("<Button-3>", lambda e: model_field_menu.tk_popup(e.x_root, e.y_root))

        def save_and_close():
            self.set_user_model(entry.get())
            win.destroy()

        ctk.CTkButton(win, text="SAVE MY MODEL", font=self.header_font, height=46,
                      fg_color="#1e8449", hover_color="#27ae60",
                      command=save_and_close).pack(pady=(18, 0))
        ctk.CTkButton(win, text="Open Codex app", font=self.main_font,
                      command=lambda: self.open_codex_handoff(win)).pack(pady=(12, 0))
        self._make_text_copyable(win)

    def open_codex_handoff(self, window):
        """Open a draft in the Codex desktop app for the configured job folder."""
        try:
            response = self._daemon_request("GET", "/", timeout=2)
            response.raise_for_status()
            job_dir = response.json().get("codex_job_dir")
        except (requests.RequestException, ValueError):
            job_dir = None
        if not job_dir:
            messagebox.showinfo("Codex desktop", "Enable Codex in Ledger setup and start the Ledger service.", parent=window)
            return

        link = "codex://new?" + urlencode({"path": job_dir, "prompt": CODEX_HANDOFF_PROMPT})
        try:
            if IS_WINDOWS:
                os.startfile(link)
                return
            if IS_MACOS:
                subprocess.run(["/usr/bin/open", link], capture_output=True, timeout=3, check=True)
                return
            handler = subprocess.run(["xdg-mime", "query", "default", "x-scheme-handler/codex"],
                                     capture_output=True, text=True, timeout=3, check=True)
            if handler.stdout.strip():
                subprocess.Popen(["xdg-open", link], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
        except (OSError, subprocess.SubprocessError):
            pass
        remote_note = ("" if self.runtime is not None else
                       "If Codex is on another computer, use its SSH connection to this appliance.\n\n")
        messagebox.showinfo(
            "Codex desktop",
            f"In Codex, open this folder as a project:\n{job_dir}\n\n"
            f"{remote_note}Select GPT-6.1 Sol or GPT-6 Astra and send:\n" + CODEX_HANDOFF_PROMPT,
            parent=window,
        )

    def open_advanced_settings(self):
        """Admin 'danger zone': Factory Reset only. The AI model is now set in ONE
        place -- the all-users AI MODEL button (open_model_picker) -- so this
        window no longer changes the model."""
        adv_win = ctk.CTkToplevel(self.root)
        adv_win.title("Reset provider setup" if self.runtime is not None else "JUST DON'T!")
        adv_win.geometry("600x340")
        adv_win.minsize(560, 300)
        adv_win.transient(self.root)

        # The Scary Warning Frame
        warn_frame = ctk.CTkFrame(adv_win, fg_color="#c0392b", corner_radius=10)  # Blaze Red
        warn_frame.pack(fill="x", padx=30, pady=(30, 20))

        warn_label = ctk.CTkLabel(
            warn_frame,
            text=("Reset removes Ledger's provider settings and keys for this user account.\n"
                  "Your grading workspace stays in place." if self.runtime is not None else
                  "WARNING!\n\nFactory reset removes Ledger's provider settings and keys\n"
                  "and re-locks the appliance. Azure CLI sign-in is separate.\n"
                  "Get out of here unless you know what you're doing!"),
            font=ctk.CTkFont(family="Ubuntu", size=16, weight="bold"),
            text_color="white"
        )
        warn_label.pack(pady=20)

        # --- ADMIN-ONLY FACTORY RESET ---
        try:
            can_reset = self.runtime is not None
            if not can_reset:
                current_user = getpass.getuser()
                sudo_group = grp.getgrnam('sudo').gr_mem
                can_reset = current_user == 'paragon' or current_user in sudo_group

            if can_reset:
                reset_btn = ctk.CTkButton(
                    adv_win,
                    text="Reset provider setup" if self.runtime is not None else "FACTORY RESET APPLIANCE",
                    font=self.main_font,
                    fg_color="#8B0000",  # Dark Red
                    hover_color="#A52A2A",
                    command=lambda: self.execute_factory_reset(adv_win)
                )
                reset_btn.pack(pady=(20, 0))
        except Exception as e:
            self.log_message(f"[!] Admin verification check failed: {e}")

        self._make_text_copyable(adv_win)

    def execute_factory_reset(self, window):
        """Wipes the secure .env and database liability signatures to lock the system."""
        if self.runtime is not None and (self.is_running or self._batch_pending):
            messagebox.showinfo("Ledger", "Stop the current batch before resetting provider setup.", parent=window)
            return
        try:
            if self.runtime is not None:
                self.runtime.reset_setup()
            else:
                self.log_message("Requesting administrative privileges for Factory Reset...")
                subprocess.run(['pkexec', 'sh', '-c', 'set -e; rm -f /etc/paragon/.env /etc/paragon/sa.json /etc/ledger-initialized; set +e; systemctl stop ledger-daemon.service; exit 0'], check=True)
            
            # 2. Scrub the signature from the SQLite database
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM Settings WHERE Setting_Key IN ('liability_signer', 'liability_institution', 'liability_timestamp')")
            conn.commit()
            conn.close()
            
            self.log_message("Provider setup reset." if self.runtime is not None else
                             "-> APPLIANCE RESET SUCCESSFUL. System locked.")
            window.destroy()
            
            # 3. Re-evaluate first-run state THROUGH the tier gate (an initializer
            #    gets the wizard; a lower tier gets the awaiting-setup notice).
            self.check_first_run()
            
        except subprocess.CalledProcessError:
            self.log_message("[!] ERROR: Administrator authorization failed. Reset aborted.")
        except Exception as e:
            self.log_message(f"[!] Database Write Error (Factory Reset): {e}")
            if self.runtime is not None:
                messagebox.showerror("Ledger setup", str(e), parent=window)

    def open_keys_and_accounts(self):
        """Launch the tier-aware Keys & Accounts GUI (ledger-keymgr-gui.py).

        This replaces the old in-app open_user_manager / open_fido2_enrollment
        windows. Those drove the retired 03-Admin-Scripts/02-enroll-fido2-user.sh,
        which ran 'dracut -f' after enrollment -- the documented cause of the June
        boot incident. The new GUI drives ledger-keytool / ledger-usermgr, which
        write only the on-disk LUKS2 header and NEVER touch the boot image
        (see system map Rev f, registers #5/#6/#23). Folding this in here also
        removes the need for the separate "Ledger Keys & Accounts" desktop launcher.
        """
        # Shared engine published to /opt/ledger (system map §11.8).
        keymgr_python = "/opt/ledger/venv/bin/python"
        keymgr_script = "/opt/ledger/engine/ledger-keymgr-gui.py"

        if not (os.path.exists(keymgr_python) and os.path.exists(keymgr_script)):
            self.log_message(
                "[!] Keys & Accounts GUI not found. Expected: "
                f"{keymgr_python} {keymgr_script}"
            )
            return

        try:
            # Run as the logged-in user (NOT sudo); the GUI calls the privileged
            # helpers itself via the prompt-free /etc/sudoers.d/ledger drop-in.
            subprocess.Popen([keymgr_python, keymgr_script])
            self.log_message("-> Opened Keys & Accounts.")
        except Exception as e:
            self.log_message(f"[!] Could not open Keys & Accounts: {e}")

    def open_fido2_enrollment(self, target_user=None):
        """Launches the FIDO2 enrollment sequence with a safety confirmation."""
        if not target_user:
            target_user = getpass.getuser()

        enroll_win = ctk.CTkToplevel(self.root)
        enroll_win.title("SYSTEM LOCKDOWN")
        enroll_win.geometry("750x450")
        enroll_win.configure(fg_color="#8B0000") 
        enroll_win.transient(self.root)
        enroll_win.protocol("WM_DELETE_WINDOW", lambda: None) 

        warning_text = (
            f"CRITICAL HARDWARE SECURING IN PROGRESS FOR: {target_user.upper()}\n\n"
            "ACTION REQUIRED: Please ensure the FIDO2 key is inserted.\n"
            "You are binding this key to both the User Profile and the Encrypted Drive.\n\n"
            "When the gold pad lights up SOLID, tap it.\n"
            "You will need to wait for the solid light and tap TWICE during this process.\n\n"
            "DO NOT REMOVE POWER OR UNPLUG THE APPLIANCE."
        )

        warn_label = ctk.CTkLabel(
            enroll_win, text=warning_text, font=ctk.CTkFont(family="Ubuntu", size=16, weight="bold"),
            text_color="white", wraplength=700
        )
        warn_label.pack(pady=(40, 20))

        progress = ctk.CTkProgressBar(enroll_win, width=500, mode="indeterminate", fg_color="#550000", progress_color="white")
        
        def enrollment_worker():
            try:
                script_path = os.path.join(ENGINE_DIR, "03-Admin-Scripts", "02-enroll-fido2-user.sh")
                cmd = ['sudo', os.path.abspath(script_path), target_user]
                result = subprocess.run(cmd, capture_output=True, text=True, check=True)
                self.root.after(0, lambda: success_callback(result.stdout))
            except subprocess.CalledProcessError as e:
                self.root.after(0, lambda: error_callback(e.stderr))
            except Exception as e:
                self.root.after(0, lambda: error_callback(str(e)))

        def success_callback(log_output):
            progress.stop()
            enroll_win.destroy()
            self.log_message(f"-> FIDO2 ENROLLMENT SUCCESSFUL for {target_user}.")

        def error_callback(error_msg):
            progress.stop()
            enroll_win.destroy()
            self.log_message(f"[!] FIDO2 ENROLLMENT FAILED for {target_user}.")
            if error_msg: self.log_message(f"[!] {error_msg}")

        def confirm_action():
            proceed_btn.pack_forget()
            progress.pack(pady=20)
            progress.start()
            threading.Thread(target=enrollment_worker, daemon=True).start()

        proceed_btn = ctk.CTkButton(
            enroll_win, text="I UNDERSTAND. PROCEED WITH BINDING.", font=self.header_font, 
            height=50, fg_color="#333333", hover_color="#555555", command=confirm_action
        )
        proceed_btn.pack(pady=20)
        
        self._make_text_copyable(enroll_win)
        enroll_win.grab_set()

    def open_user_manager(self):
        """Admin dashboard for OS User creation, deletion, and key management."""
        mgr_win = ctk.CTkToplevel(self.root)
        mgr_win.title("OS USER MANAGEMENT")
        mgr_win.geometry("800x500")
        mgr_win.transient(self.root)

        header_label = ctk.CTkLabel(mgr_win, text="OS USER ARCHITECTURE", font=self.header_font)
        header_label.pack(pady=(20, 10))

        # Treeview Configuration
        columns = ("username", "group")
        tree = ttk.Treeview(mgr_win, columns=columns, show="headings", height=8)
        style = ttk.Style()
        style.theme_use("default")
        style.configure("Treeview", background="#2b2b2b", foreground="white", fieldbackground="#2b2b2b", rowheight=25)
        style.configure("Treeview.Heading", background="#1f538d", foreground="white", font=('Ubuntu', 10, 'bold'))
        style.map('Treeview', background=[('selected', '#1e8449')])

        tree.heading("username", text="System Username")
        tree.column("username", width=400, anchor="center")
        tree.heading("group", text="Primary Access")
        tree.column("group", width=350, anchor="center")
        tree.pack(fill="x", padx=20, pady=10)

        selected_user = ctk.StringVar()

        def load_users():
            for item in tree.get_children(): tree.delete(item)
            try:
                dp_group = grp.getgrnam('district-personnel')
                for user in dp_group.gr_mem:
                    tree.insert("", "end", values=(user, "district-personnel"))
            except KeyError:
                pass

        def on_tree_select(event):
            selected = tree.selection()
            if selected:
                selected_user.set(tree.item(selected[0], 'values')[0])

        tree.bind('<<TreeviewSelect>>', on_tree_select)

        # Input & Button Frame
        action_frame = ctk.CTkFrame(mgr_win, fg_color="transparent")
        action_frame.pack(pady=10)

        new_user_entry = ctk.CTkEntry(action_frame, placeholder_text="New Username (no spaces)", width=200)
        new_user_entry.grid(row=0, column=0, padx=10, pady=10)

        def run_admin_script(action, target):
            if not target: return
            try:
                script_path = os.path.join(ENGINE_DIR, "03-Admin-Scripts", "04-manage-os-users.sh")
                cmd = ['sudo', os.path.abspath(script_path), action, target]
                subprocess.run(cmd, capture_output=True, text=True, check=True)
                self.log_message(f"-> OS Action [{action}] successful for {target}.")
                load_users()
            except subprocess.CalledProcessError as e:
                self.log_message(f"[!] OS Action [{action}] FAILED: {e.stderr}")

        ctk.CTkButton(action_frame, text="ADD USER", fg_color="#1e8449", hover_color="#27ae60", 
                      command=lambda: run_admin_script("CREATE", new_user_entry.get().strip().lower())).grid(row=0, column=1, padx=5)
        ctk.CTkButton(action_frame, text="DELETE USER", fg_color="#c0392b", hover_color="#e74c3c", 
                      command=lambda: run_admin_script("DELETE", selected_user.get())).grid(row=0, column=2, padx=5)

        # FIDO2 Management Row
        fido_frame = ctk.CTkFrame(mgr_win, fg_color="transparent")
        fido_frame.pack(pady=10)

        ctk.CTkButton(fido_frame, text="ENROLL KEY FOR SELECTED USER", fg_color="#1f538d", hover_color="#14375e", 
                      command=lambda: self.open_fido2_enrollment(target_user=selected_user.get()) if selected_user.get() else None).pack(side="left", padx=10)
        ctk.CTkButton(fido_frame, text="REVOKE DESKTOP KEY (LOST KEY)", fg_color="#d35400", hover_color="#e67e22", 
                      command=lambda: run_admin_script("REVOKE_PAM", selected_user.get())).pack(side="left", padx=10)

        load_users()
        self._make_text_copyable(mgr_win)

    # ==========================================
    # SURGERY AREA 5: THE VISUAL CALIBRATION STUDIO
    # ==========================================
    def open_calibration_tool(self, is_intercept=False):
        """Launches the interactive visual canvas to lock in OpenCV array slices."""
        
        # 1. Locate the target document (Student Scan > Master PDF > Abort)
        scan_files = [f for f in os.listdir(UNEVALUATED_DIR) if f.lower().endswith('.pdf')]
        
        target_pdf = None
        if scan_files:
            target_pdf = os.path.join(UNEVALUATED_DIR, scan_files[0])
            if not is_intercept: self.log_message(f"Calibrating using Student Scan: {scan_files[0]}")
        else:
            self.log_message("No student scans found. Calibration requires a physical scan.")
            
            # Launch a native CustomTkinter informational modal
            info_win = ctk.CTkToplevel(self.root)
            info_win.title("SCAN REQUIRED")
            info_win.geometry("500x220")
            info_win.transient(self.root)
            
            info_label = ctk.CTkLabel(
                info_win, 
                text="There is no scanned document to calibrate.\n\nPlease drop a scanned student assignment into the\n'02 Inbox Scans' folder before calibrating.", 
                font=self.main_font,
                text_color="white"
            )
            info_label.pack(pady=(40, 20))
            
            ok_btn = ctk.CTkButton(
                info_win, 
                text="OK", 
                font=self.header_font,
                height=45,
                fg_color="#1e8449", 
                hover_color="#27ae60", 
                command=info_win.destroy
            )
            ok_btn.pack(pady=(10, 0))
            
            self._make_text_copyable(info_win)
            info_win.grab_set() # Lock focus to this popup
            if is_intercept: self.shut_down()   # [PATCH:calib-close-reset] D1b: RUN cannot proceed
            return

        # 2. Render the massive native image
        try:
            doc = fitz.open(target_pdf)
            page = doc.load_page(0)
            pix = page.get_pixmap(matrix=fitz.Matrix(4.16, 4.16), alpha=False)
            temp_img_path = os.path.join(TEMP_DIR, "temp_calibration_render.png")
            pix.save(temp_img_path)
            doc.close()
        except Exception as e:
            self.log_message(f"[!] Error rendering calibration preview: {e}")
            if is_intercept: self.shut_down()   # [PATCH:calib-close-reset] D1b: RUN cannot proceed
            return

        # 3. Calculate scaling math to fit UI
        from PIL import ImageTk
        raw_img = Image.open(temp_img_path)
        native_w, native_h = raw_img.size
        
        # [PATCH:calib-fit] fit the studio to the screen (the dashboard's clamp):
        # at a fixed 800 px the page-number box and SAVE sat below the bottom of
        # a 768-px display. Stored coordinates are native pixels, so unaffected.
        target_ui_height = min(800, self.root.winfo_screenheight() - WIZARD_SCREEN_ALLOWANCE - 120)
        scale_factor = target_ui_height / native_h
        ui_w = int(native_w * scale_factor)
        
        ui_img = raw_img.resize((ui_w, target_ui_height), Image.Resampling.LANCZOS)
        
        # 4. Build the UI Canvas
        calib_win = ctk.CTkToplevel(self.root)
        calib_win.title("PARAGON SCANNER CALIBRATION STUDIO")
        calib_win.geometry(f"{ui_w + 40}x{target_ui_height + 120}")
        calib_win.transient(self.root)
        
        # [PATCH:calib-close-reset] WO2 D1: when RUN opened this studio, closing
        # it with the window's X left is_running=True and RUN disabled, so the
        # batch could never start (CALIBRATE SCANNER opens the studio save-only).
        # The X now cancels the run, exactly as SHUT DOWN does.
        if is_intercept:
            def cancel_intercept():
                calib_win.destroy()
                self.log_message("Calibration closed without saving -- run cancelled.")
                self.shut_down()
            calib_win.protocol("WM_DELETE_WINDOW", cancel_intercept)

        header_label = ctk.CTkLabel(calib_win, text="ADJUST DOCUMENT CROP ZONES", font=self.header_font)
        header_label.pack(pady=(10, 5))
        
        canvas = tk.Canvas(calib_win, width=ui_w, height=target_ui_height, bg="black", highlightthickness=0)
        canvas.pack(pady=5)
        
        # Keep a persistent reference to the image to prevent garbage collection
        calib_win.tk_img = ImageTk.PhotoImage(ui_img)
        canvas.create_image(0, 0, anchor="nw", image=calib_win.tk_img)

        # 5. Interactive LibreOffice-Style Box Logic
        class CalibrationBox:
            def __init__(self, cv, name, x, y, w, h, color):
                self.cv = cv
                self.s = 5 # Handle radius
                
                # INITIAL PLACEMENT RESTRAINT: Prevent boxes from starting off-screen
                if x + w > ui_w:
                    w = ui_w - x - 2 # 2px visual buffer
                if y + h > target_ui_height:
                    h = target_ui_height - y - 2
                
                # Floating Label (Pinned 10px above the top-left corner)
                self.label_id = cv.create_text(x, y - 10, text=name, fill=color, font=("Ubuntu", 12, "bold"), anchor="sw")
                
                # Main Body
                self.rect_id = cv.create_rectangle(x, y, x+w, y+h, outline=color, width=3, fill="", activeoutline="white")
                # 4 Corner Nodes
                self.tl = cv.create_rectangle(x-self.s, y-self.s, x+self.s, y+self.s, fill=color)
                self.tr = cv.create_rectangle(x+w-self.s, y-self.s, x+w+self.s, y+self.s, fill=color)
                self.bl = cv.create_rectangle(x-self.s, y+h-self.s, x+self.s, y+h+self.s, fill=color)
                self.br = cv.create_rectangle(x+w-self.s, y+h-self.s, x+w+self.s, y+h+self.s, fill=color)
                
                # Event Bindings
                for item in (self.rect_id, self.tl, self.tr, self.bl, self.br, self.label_id):
                    cv.tag_bind(item, "<ButtonPress-1>", self.start_action)
                    
                cv.tag_bind(self.rect_id, "<B1-Motion>", self.move_all)
                cv.tag_bind(self.label_id, "<B1-Motion>", self.move_all) # Label drag moves the box too
                cv.tag_bind(self.tl, "<B1-Motion>", lambda e: self.resize(e, "tl"))
                cv.tag_bind(self.tr, "<B1-Motion>", lambda e: self.resize(e, "tr"))
                cv.tag_bind(self.bl, "<B1-Motion>", lambda e: self.resize(e, "bl"))
                cv.tag_bind(self.br, "<B1-Motion>", lambda e: self.resize(e, "br"))

            def start_action(self, event):
                self.last_x, self.last_y = event.x, event.y

            def move_all(self, event):
                dx, dy = event.x - self.last_x, event.y - self.last_y
                
                # THE DRAG WALL: Check future coordinates to prevent pushing off canvas
                x1, y1, x2, y2 = self.cv.coords(self.rect_id)
                
                # Clamp dx to screen width
                if x1 + dx < 0: dx = -x1
                elif x2 + dx > ui_w: dx = ui_w - x2
                
                # Clamp dy (Leaves a 20px buffer at the top so the label doesn't get crushed)
                if y1 + dy < 20: dy = 20 - y1 
                elif y2 + dy > target_ui_height: dy = target_ui_height - y2
                
                for item in (self.rect_id, self.tl, self.tr, self.bl, self.br, self.label_id):
                    self.cv.move(item, dx, dy)
                    
                self.last_x, self.last_y = self.last_x + dx, self.last_y + dy

            def resize(self, event, node):
                x1, y1, x2, y2 = self.cv.coords(self.rect_id)
                
                # THE RESIZE WALL: Clamp the incoming mouse coordinates
                nx = max(0, min(event.x, ui_w))
                ny = max(20, min(event.y, target_ui_height)) # 20px buffer for label clearance
                
                # Collision logic prevents inverting the box
                if node == "tl" and nx < x2-20 and ny < y2-20: x1, y1 = nx, ny
                elif node == "tr" and nx > x1+20 and ny < y2-20: x2, y1 = nx, ny
                elif node == "bl" and nx < x2-20 and ny > y1+20: x1, y2 = nx, ny
                elif node == "br" and nx > x1+20 and ny > y1+20: x2, y2 = nx, ny
                else: return

                self.cv.coords(self.rect_id, x1, y1, x2, y2)
                self.cv.coords(self.label_id, x1, y1 - 10) # Keep label pinned during resize
                self.cv.coords(self.tl, x1-self.s, y1-self.s, x1+self.s, y1+self.s)
                self.cv.coords(self.tr, x2-self.s, y1-self.s, x2+self.s, y1+self.s)
                self.cv.coords(self.bl, x1-self.s, y2-self.s, x1+self.s, y2+self.s)
                self.cv.coords(self.br, x2-self.s, y2-self.s, x2+self.s, y2+self.s)
                
            def get_coords(self):
                x1, y1, x2, y2 = self.cv.coords(self.rect_id)
                return (int(x1), int(y1), int(x2 - x1), int(y2 - y1))

        # 6. Fetch native coords and generate the boxes
        # [PATCH:calib-round] round(), not int(), here and in save_and_proceed:
        # truncating both ways cost ~4 native px per open+save, so untouched
        # boxes crept up-left and shrank on every RUN.
        t_coords = self.get_crop_coordinates('crop_title')
        id_coords = self.get_crop_coordinates('crop_id')
        f_coords = self.get_crop_coordinates('crop_footer')
        
        box_title = CalibrationBox(canvas, "Title", round(t_coords[0]*scale_factor), round(t_coords[1]*scale_factor), round(t_coords[2]*scale_factor), round(t_coords[3]*scale_factor), "#3498db") # Blue
        box_id = CalibrationBox(canvas, "ID", round(id_coords[0]*scale_factor), round(id_coords[1]*scale_factor), round(id_coords[2]*scale_factor), round(id_coords[3]*scale_factor), "#2ecc71") # Green
        box_footer = CalibrationBox(canvas, "Page Number", round(f_coords[0]*scale_factor), round(f_coords[1]*scale_factor), round(f_coords[2]*scale_factor), round(f_coords[3]*scale_factor), "#e74c3c") # Red
        
        # 7. Save Protocol
        def save_and_proceed():
                # Get UI coords and mathematically scale them back up to massive native resolution
                t_ui = box_title.get_coords()
                id_ui = box_id.get_coords()
                f_ui = box_footer.get_coords()
                
                t_nat = f"{round(t_ui[0]/scale_factor)},{round(t_ui[1]/scale_factor)},{round(t_ui[2]/scale_factor)},{round(t_ui[3]/scale_factor)}"
                id_nat = f"{round(id_ui[0]/scale_factor)},{round(id_ui[1]/scale_factor)},{round(id_ui[2]/scale_factor)},{round(id_ui[3]/scale_factor)}"
                f_nat = f"{round(f_ui[0]/scale_factor)},{round(f_ui[1]/scale_factor)},{round(f_ui[2]/scale_factor)},{round(f_ui[3]/scale_factor)}"
                
                try:
                    conn = sqlite3.connect(DB_PATH)
                    cursor = conn.cursor()
                    cursor.execute("UPDATE Settings SET Setting_Value = ? WHERE Setting_Key = 'crop_title'", (t_nat,))
                    cursor.execute("UPDATE Settings SET Setting_Value = ? WHERE Setting_Key = 'crop_id'", (id_nat,))
                    cursor.execute("UPDATE Settings SET Setting_Value = ? WHERE Setting_Key = 'crop_footer'", (f_nat,))
                    conn.commit()
                    conn.close()
                    self.log_message("-> Calibration Zones Locked & Saved.")
                    self.calibrated_this_session = True   # [PATCH:calib-session-skip]
                except Exception as e:
                    self.log_message(f"[!] DB Error saving coordinates: {e}")
                    
                calib_win.destroy()
                
                # Hook logic for Phase 3 Batch Intercept
                if is_intercept:
                    self.log_message("Calibration settings saved. Starting Ledger engine...")
                    # ARCHITECTURAL FIX: Push the blocking API extraction into a background thread
                    self._start_batch_worker(target_pdf)
                
        save_btn = ctk.CTkButton(calib_win, text="SAVE & PROCEED" if is_intercept else "SAVE", font=self.header_font, height=50, fg_color="#1e8449", hover_color="#27ae60", command=save_and_proceed)
        save_btn.pack(pady=(10, 20))
        
        self._make_text_copyable(calib_win)
        calib_win.grab_set()

    def open_roster_manager(self):
        """Launches the dedicated appliance GUI for managing the SQLite Roster and FERPA Pseudonyms."""
        rost_win = ctk.CTkToplevel(self.root)
        rost_win.title("ROSTER MANAGER")
        rost_win.geometry("1100x850") # Expanded height to fit the legal block
        rost_win.transient(self.root)
        
        custom_label = self.get_custom_id_label()
        
        header_label = ctk.CTkLabel(rost_win, text="ROSTER & PRIVACY MANAGEMENT", font=self.header_font)
        header_label.pack(pady=(20, 10))

        # --- FERPA & PSEUDONYM HEADER BLOCK ---
        ferpa_frame = ctk.CTkFrame(rost_win, fg_color="#1a1a1a", corner_radius=10)
        ferpa_frame.pack(fill="x", padx=20, pady=(0, 15))
        
        # 1. Input Field & Tooltip
        input_row = ctk.CTkFrame(ferpa_frame, fg_color="transparent")
        input_row.pack(fill="x", padx=20, pady=(15, 5))
        
        ctk.CTkLabel(input_row, text="Pseudonym System:", font=self.main_font).pack(side="left")
        ctk.CTkLabel(input_row, text="(e.g., \"Ms. Brown's Class Code\" or \"Brownian ID\")", font=ctk.CTkFont(family="Ubuntu", size=12, slant="italic"), text_color="gray").pack(side="left", padx=10)
        
        custom_label_entry = ctk.CTkEntry(input_row, font=self.main_font, width=300)
        custom_label_entry.insert(0, custom_label)
        custom_label_entry.pack(side="left", padx=10)
        
        # Subtext Warning
        warning_label = ctk.CTkLabel(
            ferpa_frame, 
            text="*Do not name your pseudonym system \"STUDENT ID.\" It must be YOUR OWN numbering system, NOT students' DISTRICT-ISSUED student IDs.", 
            font=ctk.CTkFont(family="Ubuntu", size=13, weight="bold"), 
            text_color="#c0392b"
        )
        warning_label.pack(anchor="w", padx=20, pady=(0, 10))
        
        # 2. FERPA Legal Text Box
        ferpa_text = (
            "DATA PRIVACY AND FERPA COMPLIANCE\n\n"
            "The Ledger is designed to process student artifacts while maintaining strict data anonymity. To ensure compliance "
            "with the Family Educational Rights and Privacy Act (FERPA) and local data privacy policies, the use of Personally "
            "Identifiable Information (PII) is strictly prohibited. You must assign randomized or localized proxy numbers to your students.\n\n"
            "You may not under any circumstances utilize students' real names, partial names, or district-issued identification "
            "numbers as the primary identifier within this system."
        )
        ferpa_box = ctk.CTkTextbox(ferpa_frame, height=130, wrap="word", font=ctk.CTkFont(family="Ubuntu", size=12))
        ferpa_box.pack(fill="x", padx=20, pady=(0, 10))
        ferpa_box.insert("0.0", ferpa_text)
        ferpa_box.configure(state="disabled")
        
        # 3. Liability Checkbox & Dynamic Button
        action_row = ctk.CTkFrame(ferpa_frame, fg_color="transparent")
        action_row.pack(fill="x", padx=20, pady=(0, 15))
        
        check_var = ctk.StringVar(value="off")
        
        def toggle_update_btn():
            if check_var.get() == "on":
                save_label_btn.configure(state="normal", fg_color="#1e8449", hover_color="#27ae60")
            else:
                save_label_btn.configure(state="disabled", fg_color="#333333")
                
        def on_entry_edit(*args):
            # Uncheck box and disable button if they alter the text
            check_var.set("off")
            toggle_update_btn()
            
        custom_label_entry.bind("<KeyRelease>", on_entry_edit)

        liability_cb = ctk.CTkCheckBox(
            action_row, 
            text="I have read the FERPA compliance statement and understand that students’ personally identifying information (PII)\nmust be pseudonymized before using The Ledger system. I accept full legal responsibility for ensuring that my chosen\nPseudonym System does not contain PII or district-issued ID numbers.",
            variable=check_var, onvalue="on", offvalue="off",
            font=ctk.CTkFont(family="Ubuntu", size=11, weight="bold"),
            text_color="white",
            command=toggle_update_btn
        )
        liability_cb.pack(side="left", fill="x", expand=True, padx=(0, 10))
        
        def save_pseudonym():
            new_label = custom_label_entry.get().strip()
            if not new_label: new_label = "Student Pseudonym" 
            try:
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                # Insert or replace seamlessly handles the new database key
                cursor.execute("INSERT OR REPLACE INTO Settings (Setting_Key, Setting_Value) VALUES ('custom_id_label', ?)", (new_label,))
                conn.commit()
                conn.close()
                self.log_message(f"-> Pseudonym System Updated to: '{new_label}'. Liability accepted.")
                
                # Dynamically update the tree header and input label without rebooting the window
                tree.heading("b_id", text=new_label)
                b_id_label.configure(text=f"{new_label}:")
                
                # Reset friction mechanism
                check_var.set("off")
                toggle_update_btn()
                
            except Exception as e:
                self.log_message(f"[!] Database Update Error (Pseudonym): {e}")

        save_label_btn = ctk.CTkButton(
            action_row, 
            text="SAVE & UPDATE", 
            font=self.header_font,
            height=45,
            width=170,
            fg_color="#333333", # Starts disabled
            state="disabled",
            command=save_pseudonym
        )
        save_label_btn.pack(side="right")
        # ----------------------------------------
        
        # Treeview Configuration
        columns = ("b_id", "first", "last")
        tree = ttk.Treeview(rost_win, columns=columns, show="headings", height=10)
        
        style = ttk.Style()
        style.theme_use("default")
        style.configure("Treeview", background="#2b2b2b", foreground="white", fieldbackground="#2b2b2b", rowheight=25)
        style.configure("Treeview.Heading", background="#1f538d", foreground="white", font=('Ubuntu', 10, 'bold'))
        style.map('Treeview', background=[('selected', '#1e8449')])
        
        tree.heading("b_id", text=custom_label)
        tree.column("b_id", width=160, anchor="center")
        tree.heading("first", text="First Name")
        tree.column("first", width=180)
        tree.heading("last", text="Last Name")
        tree.column("last", width=180)
                
        tree.pack(fill="x", padx=20, pady=10)
        
        # Input Controls Frame
        input_frame = ctk.CTkFrame(rost_win, fg_color="transparent")
        input_frame.pack(fill="x", padx=20, pady=10)
        
        # Row 1 Inputs
        row1 = ctk.CTkFrame(input_frame, fg_color="transparent")
        row1.pack(fill="x", pady=5)
        
        b_id_label = ctk.CTkLabel(row1, text=f"{custom_label}:", width=140, anchor="e")
        b_id_label.pack(side="left", padx=5)
        b_id_entry = ctk.CTkEntry(row1, width=100)
        b_id_entry.pack(side="left", padx=5)
        
        ctk.CTkLabel(row1, text="First Name:", width=80, anchor="e").pack(side="left", padx=5)
        first_entry = ctk.CTkEntry(row1, width=150)
        first_entry.pack(side="left", padx=5)
        
        ctk.CTkLabel(row1, text="Last Name:", width=80, anchor="e").pack(side="left", padx=5)
        last_entry = ctk.CTkEntry(row1, width=150)
        last_entry.pack(side="left", padx=5)
                             
        # Action Buttons
        btn_frame = ctk.CTkFrame(rost_win, fg_color="transparent")
        btn_frame.pack(pady=10)

        def apply_entry_menu(entry_widget):
            def custom_entry_paste():
                try:
                    entry_widget.insert("insert", rost_win.clipboard_get())
                except tk.TclError:
                    pass
            menu = tk.Menu(entry_widget, tearoff=False, bg="#2b2b2b", fg="white")
            menu.add_command(label="Copy", command=lambda: entry_widget.event_generate("<<Copy>>"))
            menu.add_command(label="Paste", command=custom_entry_paste)
            entry_widget.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))

        for entry in [b_id_entry, first_entry, last_entry, custom_label_entry]:
            apply_entry_menu(entry)

        def load_roster_data():
            for item in tree.get_children():
                tree.delete(item)
            try:
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                cursor.execute("SELECT Bartonian_ID, First_Name, Last_Name FROM Roster ORDER BY Last_Name, First_Name")
                for row in cursor.fetchall():
                    tree.insert("", "end", values=row)
                conn.close()
            except Exception as e:
                self.log_message(f"[!] Roster DB Read Error: {e}")

        def on_tree_select(event):
            selected = tree.selection()
            if selected:
                values = tree.item(selected[0], 'values')
                b_id_entry.delete(0, 'end'); b_id_entry.insert(0, values[0])
                first_entry.delete(0, 'end'); first_entry.insert(0, values[1])
                last_entry.delete(0, 'end'); last_entry.insert(0, values[2])
                
        def add_student():
            try:
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO Roster (Bartonian_ID, First_Name, Last_Name)
                    VALUES (?, ?, ?)
                ''', (b_id_entry.get().strip(), first_entry.get().strip(), last_entry.get().strip()))
                conn.commit()
                conn.close()
                load_roster_data() 
                self.roster = self.load_roster() 
                self.log_message(f"-> Added ID {b_id_entry.get()} to database.")
            except sqlite3.IntegrityError:
                self.log_message(f"[!] Cannot Add: Pseudonym ID must be unique.")
            except Exception as e:
                self.log_message(f"[!] Roster Add Error: {e}")

        def update_student():
            try:
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE Roster 
                    SET First_Name=?, Last_Name=?
                    WHERE Bartonian_ID=?
                ''', (first_entry.get().strip(), last_entry.get().strip(), b_id_entry.get().strip()))
                conn.commit()
                conn.close()
                load_roster_data()
                self.roster = self.load_roster() 
                self.log_message(f"-> Updated ID {b_id_entry.get()} in database.")
            except Exception as e:
                self.log_message(f"[!] Roster Update Error: {e}")

        def delete_student():
            b_id = b_id_entry.get().strip()
            try:
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                cursor.execute("DELETE FROM Roster WHERE Bartonian_ID=?", (b_id,))
                cursor.execute("DELETE FROM Grades WHERE Bartonian_ID=?", (b_id,))
                conn.commit()
                conn.close()
                load_roster_data()
                self.roster = self.load_roster() 
                self.log_message(f"-> Safely purged ID {b_id} and associated grades from system.")
            except Exception as e:
                self.log_message(f"[!] Roster Delete Error: {e}")

        tree.bind('<<TreeviewSelect>>', on_tree_select)
        
        ctk.CTkButton(btn_frame, text="ADD STUDENT", fg_color="#1f538d", hover_color="#14375e", command=add_student).pack(side="left", padx=10)
        ctk.CTkButton(btn_frame, text="UPDATE STUDENT", fg_color="#d35400", hover_color="#e67e22", command=update_student).pack(side="left", padx=10)
        ctk.CTkButton(btn_frame, text="DELETE STUDENT", fg_color="#c0392b", hover_color="#e74c3c", command=delete_student).pack(side="left", padx=10)
        
        load_roster_data()
        self._make_text_copyable(rost_win)

    def open_assignment_manager(self):
        """Launches the dedicated appliance GUI for managing the SQLite Assignments."""
        assn_win = ctk.CTkToplevel(self.root)
        assn_win.title("ASSIGNMENT MANAGER")
        assn_win.geometry("800x500")
        assn_win.transient(self.root)

        header_label = ctk.CTkLabel(assn_win, text="ASSIGNMENT MANAGEMENT", font=self.header_font)
        header_label.pack(pady=(20, 10))

        # Treeview Configuration
        columns = ("a_key", "title", "max_score")
        tree = ttk.Treeview(assn_win, columns=columns, show="headings", height=8)

        style = ttk.Style()
        style.theme_use("default")
        style.configure("Treeview", background="#2b2b2b", foreground="white", fieldbackground="#2b2b2b", rowheight=25)
        style.configure("Treeview.Heading", background="#1f538d", foreground="white", font=('Ubuntu', 10, 'bold'))
        style.map('Treeview', background=[('selected', '#1e8449')])

        tree.heading("a_key", text="System Key")
        tree.column("a_key", width=0, stretch=False) # Hides internal key from user
        tree.heading("title", text="Display Title")
        tree.column("title", width=400)
        tree.heading("max_score", text="Max Score")
        tree.column("max_score", width=150, anchor="center")

        tree.pack(fill="x", padx=20, pady=10)

        # Input Controls Frame
        input_frame = ctk.CTkFrame(assn_win, fg_color="transparent")
        input_frame.pack(fill="x", padx=20, pady=10)

        ctk.CTkLabel(input_frame, text="Display Title:", width=100, anchor="e").pack(side="left", padx=5)
        title_entry = ctk.CTkEntry(input_frame, width=300)
        title_entry.pack(side="left", padx=5)

        ctk.CTkLabel(input_frame, text="Max Score:", width=80, anchor="e").pack(side="left", padx=5)
        score_entry = ctk.CTkEntry(input_frame, width=80)
        score_entry.pack(side="left", padx=5)

        # SURGICAL INSERTION: Universal Right-Click Menu for Assignment Entries
        def apply_assn_menu(entry_widget):
            def custom_assn_paste():
                try:
                    entry_widget.insert("insert", assn_win.clipboard_get())
                except tk.TclError:
                    pass
            menu = tk.Menu(entry_widget, tearoff=False, bg="#2b2b2b", fg="white")
            menu.add_command(label="Copy", command=lambda: entry_widget.event_generate("<<Copy>>"))
            menu.add_command(label="Paste", command=custom_assn_paste)
            entry_widget.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))

        for entry in [title_entry, score_entry]:
            apply_assn_menu(entry)

        # Hidden variable to track which assignment is selected for DB queries
        selected_key = ctk.StringVar()

        # Action Buttons
        btn_frame = ctk.CTkFrame(assn_win, fg_color="transparent")
        btn_frame.pack(pady=20)

        # --- INTERNAL LOGIC FUNCTIONS ---
        def load_assignment_data():
            for item in tree.get_children():
                tree.delete(item)
            try:
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                cursor.execute("SELECT Assignment_Key, Display_Title, Max_Score FROM Assignments ORDER BY Display_Title")
                for row in cursor.fetchall():
                    tree.insert("", "end", values=row)
                conn.close()
            except Exception as e:
                self.log_message(f"[!] Assignment DB Read Error: {e}")

        def on_tree_select(event):
            selected = tree.selection()
            if selected:
                values = tree.item(selected[0], 'values')
                selected_key.set(values[0])
                title_entry.delete(0, 'end'); title_entry.insert(0, values[1])
                # [PATCH:score-format] Intentionally UNformatted: this field is editable and round-trips to storage via float().
                score_entry.delete(0, 'end'); score_entry.insert(0, values[2])

        def update_assignment():
            a_key = selected_key.get()
            if not a_key: return
            try:
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE Assignments
                    SET Display_Title=?, Max_Score=?
                    WHERE Assignment_Key=?
                ''', (title_entry.get().strip(), float(score_entry.get().strip()), a_key))
                conn.commit()
                conn.close()
                load_assignment_data()
                self.log_message(f"-> Updated Assignment: {title_entry.get()}")
            except Exception as e:
                self.log_message(f"[!] Assignment Update Error: {e}")

        def delete_assignment():
            a_key = selected_key.get()
            if not a_key: return
            try:
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                # Cascading delete automatically wipes associated grades from Grades table
                cursor.execute("DELETE FROM Assignments WHERE Assignment_Key=?", (a_key,))
                conn.commit()
                conn.close()
                
                load_assignment_data()
                title_entry.delete(0, 'end')
                score_entry.delete(0, 'end')
                selected_key.set("")
                self.log_message(f"-> Safely purged Assignment and all associated grades.")
            except Exception as e:
                self.log_message(f"[!] Assignment Delete Error: {e}")

        tree.bind('<<TreeviewSelect>>', on_tree_select)

        ctk.CTkButton(btn_frame, text="UPDATE ASSIGNMENT", fg_color="#d35400", hover_color="#e67e22", command=update_assignment).pack(side="left", padx=10)
        ctk.CTkButton(btn_frame, text="DELETE ASSIGNMENT", fg_color="#c0392b", hover_color="#e74c3c", command=delete_assignment).pack(side="left", padx=10)

        load_assignment_data()
        self._make_text_copyable(assn_win)

    def open_prompt_editor(self):
        """Launches the GRADING INSTRUCTIONS editor (the teacher's prompt)."""
        editor_win = ctk.CTkToplevel(self.root)
        editor_win.title("GRADING INSTRUCTIONS")
        editor_win.geometry("900x700")
        editor_win.transient(self.root) 
        
        # Grid Configuration (Weight shifted to row 2)
        editor_win.grid_rowconfigure(2, weight=1)
        editor_win.grid_columnconfigure(0, weight=1)

        # Header
        header_label = ctk.CTkLabel(editor_win, text="GRADING INSTRUCTIONS", font=self.header_font)
        header_label.grid(row=0, column=0, pady=(20, 10))

        # Row 1 is intentionally empty ([PATCH:no-temperature] the sampling
        # control that lived there was removed); an empty grid row has zero
        # height, so the editor sits directly under the header.

        # The Text Editor Area (Shifted to row 2)
        self.prompt_textbox = ctk.CTkTextbox(
            editor_win, 
            font=ctk.CTkFont(family="Ubuntu", size=14),
            wrap="word",
            corner_radius=10
        )
        self.prompt_textbox.grid(row=2, column=0, sticky="nsew", padx=20, pady=10)

        # SURGICAL INSERTION: Native Right-Click Menu (Linux Robust Version)
        def custom_paste():
            try:
                # Forcefully grab the main OS clipboard contents
                clipboard_text = editor_win.clipboard_get()
                
                # Purge any highlighted text before pasting
                if self.prompt_textbox.tag_ranges("sel"):
                    self.prompt_textbox.delete("sel.first", "sel.last")
                    
                # Insert clipboard at the current cursor position
                self.prompt_textbox.insert("insert", clipboard_text)
            except tk.TclError:
                pass # Fails gracefully if the clipboard is completely empty

        editor_menu = tk.Menu(self.prompt_textbox, tearoff=False, bg="#2b2b2b", fg="white")
        editor_menu.add_command(label="Copy", command=lambda: self.prompt_textbox.event_generate("<<Copy>>"))
        editor_menu.add_command(label="Paste", command=custom_paste)
        editor_menu.add_command(label="Select All", command=lambda: self.prompt_textbox.tag_add("sel", "1.0", "end"))
        
        self.prompt_textbox.bind("<Button-3>", lambda event: editor_menu.tk_popup(event.x_root, event.y_root))

        # [PATCH:no-default-prompt] Load the saved instructions from the DB.
        # Empty on a fresh install; a read error leaves the box empty (it is
        # logged) so error text can never be saved back as a prompt.
        self.prompt_textbox.insert("0.0", self.get_grading_instructions())

        # Save Button
        save_btn = ctk.CTkButton(
            editor_win, 
            text="SAVE PROMPT", 
            font=self.header_font,
            height=50,
            fg_color="#333333", 
            hover_color="#555555",
            command=lambda: self.save_prompt(editor_win)
        )
        save_btn.grid(row=3, column=0, pady=(10, 20), padx=20, sticky="ew")
        self._make_text_copyable(editor_win)

    def save_prompt(self, window):
        """Writes the textbox contents to the Settings DB ('grading_instructions')."""
        new_prompt_text = self.prompt_textbox.get("0.0", "end").strip()

        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO Settings (Setting_Key, Setting_Value)
                VALUES ('grading_instructions', ?)
            ''', (new_prompt_text,))
            conn.commit()
            conn.close()

            self.log_message(f"-> Grading instructions saved ({len(new_prompt_text)} characters).")
            if not self.is_running:
                self.update_indicator("idle")   # clears a NO GRADING INSTRUCTIONS banner
            window.destroy()
        except Exception as e:
            self.log_message(f"[!] Database Write Error (Grading Instructions): {e}")

    def open_gradebook(self):
        """Launches the Visual SIS interface and populates it from SQLite."""
        gb_win = ctk.CTkToplevel(self.root)
        gb_win.title("MASTER GRADEBOOK")
        gb_win.geometry("1000x600")
        gb_win.transient(self.root) # Keeps window on top
        
        ctrl_frame = ctk.CTkFrame(gb_win, fg_color="transparent")
        ctrl_frame.pack(fill="x", padx=20, pady=10)
        
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            
            cursor.execute("SELECT Assignment_Key, Display_Title, Max_Score FROM Assignments")
            assignments = cursor.fetchall()
            
            cursor.execute("SELECT Bartonian_ID, Last_Name, First_Name FROM Roster ORDER BY Last_Name, First_Name")
            students = cursor.fetchall()
            
            cursor.execute("SELECT Bartonian_ID, Assignment_Key, Score_Earned FROM Grades")
            grades = cursor.fetchall()
            conn.close()
        except Exception as e:
            self.log_message(f"[!] Gradebook DB Error: {e}")
            return
            
        # Structure grades into a dictionary: grades_dict[bartonian_id][assignment_key] = score
        grades_dict = {}
        for b_id, a_key, score in grades:
            if b_id not in grades_dict: grades_dict[b_id] = {}
            grades_dict[b_id][a_key] = score
            
        # Build the Treeview Spreadsheet
        columns = ["b_id", "last", "first"] + [a[0] for a in assignments]
        tree = ttk.Treeview(gb_win, columns=columns, show="headings")
        
        # Style the Treeview for a dark theme
        style = ttk.Style()
        style.theme_use("default")
        style.configure("Treeview", background="#2b2b2b", foreground="white", fieldbackground="#2b2b2b", rowheight=25)
        style.configure("Treeview.Heading", background="#1f538d", foreground="white", font=('Ubuntu', 10, 'bold'))
        style.map('Treeview', background=[('selected', '#1e8449')])
        
        custom_label = self.get_custom_id_label()
        tree.heading("b_id", text=custom_label)
        tree.column("b_id", width=120, anchor="center")
        tree.heading("last", text="Last Name")
        tree.column("last", width=150)
        tree.heading("first", text="First Name")
        tree.column("first", width=150)
        
        assignments_map = {"Select Assignment...": None}
        for a_key, title, max_val in assignments:
            tree.heading(a_key, text=f"{title} ({format_score(max_val)})")  # [PATCH:score-format]
            tree.column(a_key, width=150, anchor="center")
            assignments_map[title] = a_key
            
        for stu in students:
            b_id = stu[0]
            row_data = [stu[0], stu[1], stu[2]]
            for a in assignments:
                a_key = a[0]
                score = grades_dict.get(b_id, {}).get(a_key, "-")
                row_data.append(format_score(score))  # [PATCH:score-format] display only
            tree.insert("", "end", values=row_data)
            
        tree.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        
        # --- SIS Export Controls ---
        export_label = ctk.CTkLabel(ctrl_frame, text="SIS Export:", font=self.main_font)
        export_label.pack(side="left", padx=(0, 10))
        
        selected_assignment = ctk.StringVar(value="Select Assignment...")

        def toggle_export_btn(choice):
            if choice == "Select Assignment...":
                export_btn.configure(state="disabled", fg_color="#333333")
            else:
                export_btn.configure(state="normal", fg_color="#1f538d", hover_color="#14375e")

        dropdown = ctk.CTkOptionMenu(ctrl_frame, variable=selected_assignment, values=list(assignments_map.keys()), width=200, command=toggle_export_btn)
        dropdown.pack(side="left", padx=(0, 10))
        
        export_btn = ctk.CTkButton(ctrl_frame, text="GENERATE CSV", state="disabled", fg_color="#333333", 
                                   command=lambda: self.export_to_sis(selected_assignment.get(), assignments_map))
        export_btn.pack(side="left", padx=(0, 10))
        self._make_text_copyable(gb_win)
        
    def export_to_sis(self, selected_title, assignments_map):
        if selected_title == "Select Assignment...":
            self.log_message("[!] Please select an assignment to export.")
            return

        a_key = assignments_map[selected_title]

        # --- THE INTERCEPTION MODAL ---
        warn_win = ctk.CTkToplevel(self.root)
        warn_win.title("EDUCATOR VERIFICATION REQUIRED")
        warn_win.geometry("850x400") # Widened to ensure the first sentence fits on one line
        warn_win.transient(self.root)

        legal_text = (
            "THE LEDGER IS AN AUTOMATED EVALUATION SYSTEM POWERED BY ARTIFICIAL INTELLIGENCE (AI).\n\n"
            "AI MODELS CAN MAKE MISTAKES. THIS SYSTEM IS DESIGNED TO ASSIST, NOT REPLACE, CERTIFIED "
            "EDUCATORS. THE VERACITY AND COMPLETENESS OF SCORES PRODUCED BY THE LEDGER MUST BE "
            "INDEPENDENTLY EVALUATED BY THE LICENSED TEACHER OF RECORD."
        )

        warn_label = ctk.CTkLabel(
            warn_win, 
            text=legal_text, 
            font=ctk.CTkFont(family="Ubuntu", size=15, weight="bold"),
            text_color="#c0392b",
            wraplength=780 # Increased to prevent "(AI)" from becoming an orphan word
        )
        warn_label.pack(pady=(40, 20))

        # --- THE RESTORED EXPORT LOGIC ---
        def execute_export():
            try:
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT Grades.Bartonian_ID, Grades.Score_Earned, Roster.Last_Name, Roster.First_Name 
                    FROM Grades 
                    JOIN Roster ON Grades.Bartonian_ID = Roster.Bartonian_ID 
                    WHERE Grades.Assignment_Key = ?
                    ORDER BY Roster.Last_Name, Roster.First_Name
                ''', (a_key,))
                export_data = cursor.fetchall()
                conn.close()
                
                if not export_data:
                    self.log_message(f"[!] No grades found in database for {selected_title}")
                    warn_win.destroy()
                    return
                    
                export_path = os.path.join(SIS_EXPORTS_DIR, f"Grade_Export_{a_key}.csv")
                with open(export_path, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow([self.get_custom_id_label(), "Score", "Last Name", "First Name"])
                    for row in export_data:
                        writer.writerow([row[0], format_score(row[1]), row[2], row[3]])  # [PATCH:score-format] export text only; DB untouched
                            
                self.log_message(f"-> Local Grade Export isolated and saved: {export_path}")
            except Exception as e:
                self.log_message(f"[!] Export Error: {e}")
            finally:
                # Always release the window focus, even if the export fails
                warn_win.grab_release()
                warn_win.destroy()

        # --- LIABILITY CHECKBOX MECHANISM ---
        check_var = ctk.StringVar(value="off")
        
        def toggle_export_btn():
            if check_var.get() == "on":
                # Unlocks the button and turns it the system's safety green
                confirm_btn.configure(state="normal", fg_color="#1e8449", hover_color="#27ae60")
            else:
                # Locks the button and grays it out
                confirm_btn.configure(state="disabled", fg_color="#333333")

        liability_cb = ctk.CTkCheckBox(
            warn_win, 
            text="I UNDERSTAND THE LIMITATIONS OF THIS AI SYSTEM. I HAVE INDEPENDENTLY VERIFIED\nTHE ACCURACY OF THESE SCORES AND ASSUME FULL LIABILITY FOR EXPORTING THEM.",
            variable=check_var, onvalue="on", offvalue="off",
            font=ctk.CTkFont(family="Ubuntu", size=12, weight="bold"),
            text_color="white",
            command=toggle_export_btn
        )
        liability_cb.pack(pady=(10, 25))

        confirm_btn = ctk.CTkButton(
            warn_win, 
            text="ACCEPT & EXPORT", 
            font=self.header_font, 
            height=50, 
            fg_color="#333333", # Starts disabled
            state="disabled",
            command=execute_export
        )
        confirm_btn.pack()
        
        self._make_text_copyable(warn_win)
        warn_win.grab_set() # Forces focus ONLY AFTER everything is fully drawn

    def setup_logging(self):
        os.makedirs(DIAGNOSTIC_DIR, exist_ok=True)
        logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format='%(asctime)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
        self.log_message("EXPERIMENTAL EVALUATION SOFTWARE. Provided AS IS, with no warranty. "
                         "Use only with your institution's approval. Every score must be reviewed "
                         "by a licensed educator. See DISCLAIMER.md.")  # [PATCH:evaluation-wording]
        self.log_message("System initialized. Awaiting Master PDF and Max Score.")

    def log_message(self, message):
        logging.info(message)
        self.root.after(0, self._append_to_gui, message)
        
    def _append_to_gui(self, message):
        self.log_text.insert("end", f"[{time.strftime('%H:%M:%S')}] {message}\n")
        self.log_text.see("end")

    # ==========================================================================
    # UNIVERSAL MOUSE-ONLY COPY / PASTE
    # ------------------------------------------------------------------------
    # One mechanism for every window: a right-click menu on every text-bearing
    # widget -- editable entries, textboxes (including read-only/"disabled"
    # ones), and hardcoded message labels. No Ctrl+C / Ctrl+V needed. Call
    # self._make_text_copyable(window) ONCE at the very end of a window builder,
    # AFTER every widget is created and any .configure(state="disabled") has run
    # (so read-only textboxes are seen as read-only). The walk is fully
    # defensive: a failure on any single widget never blocks the window.
    # ==========================================================================
    def _add_entry_menu(self, widget):
        """Right-click Cut/Copy/Paste/Select-All for a CTkEntry (mouse only)."""
        tkw = getattr(widget, "_entry", widget)
        menu = tk.Menu(tkw, tearoff=False, bg="#2b2b2b", fg="white")
        menu.add_command(label="Cut", command=lambda: tkw.event_generate("<<Cut>>"))
        menu.add_command(label="Copy", command=lambda: tkw.event_generate("<<Copy>>"))
        menu.add_command(label="Paste", command=lambda: tkw.event_generate("<<Paste>>"))
        menu.add_separator()

        def _select_all():
            try:
                tkw.select_range(0, "end")
                tkw.icursor("end")
            except tk.TclError:
                pass

        menu.add_command(label="Select All", command=_select_all)

        def _popup(e):
            try:
                menu.tk_popup(e.x_root, e.y_root)
            finally:
                menu.grab_release()

        tkw.bind("<Button-3>", _popup)

    def _add_textbox_menu(self, widget, editable=True):
        """Right-click menu for a CTkTextbox (mouse only). Works even when the
        textbox is read-only ('disabled'): if nothing is selected, Copy grabs
        the entire contents, so baked-in disclaimer text is always copyable."""
        tkw = getattr(widget, "_textbox", widget)
        win = widget.winfo_toplevel()
        menu = tk.Menu(tkw, tearoff=False, bg="#2b2b2b", fg="white")

        def _copy():
            try:
                text = tkw.get("sel.first", "sel.last")
            except tk.TclError:
                text = tkw.get("1.0", "end-1c")  # nothing selected -> copy it all
            if text:
                win.clipboard_clear()
                win.clipboard_append(text)

        menu.add_command(label="Copy", command=_copy)

        if editable:
            def _paste():
                try:
                    clip = win.clipboard_get()
                except tk.TclError:
                    return
                try:
                    if tkw.tag_ranges("sel"):
                        tkw.delete("sel.first", "sel.last")
                except tk.TclError:
                    pass
                tkw.insert("insert", clip)

            def _cut():
                _copy()
                try:
                    if tkw.tag_ranges("sel"):
                        tkw.delete("sel.first", "sel.last")
                except tk.TclError:
                    pass

            menu.add_command(label="Cut", command=_cut)
            menu.add_command(label="Paste", command=_paste)

        menu.add_separator()

        def _select_all():
            try:
                tkw.tag_add("sel", "1.0", "end-1c")
            except tk.TclError:
                pass

        menu.add_command(label="Select All", command=_select_all)

        def _popup(e):
            try:
                menu.tk_popup(e.x_root, e.y_root)
            finally:
                menu.grab_release()

        tkw.bind("<Button-3>", _popup)

    def _add_label_copy(self, widget):
        """Right-click 'Copy text' for a hardcoded message label (mouse only).
        Labels can't be highlighted, so this copies the label's full text."""
        tkw = getattr(widget, "_label", widget)
        win = widget.winfo_toplevel()
        menu = tk.Menu(tkw, tearoff=False, bg="#2b2b2b", fg="white")

        def _copy():
            try:
                text = widget.cget("text")
            except Exception:
                text = ""
            if text:
                win.clipboard_clear()
                win.clipboard_append(text)

        menu.add_command(label="Copy text", command=_copy)

        def _popup(e):
            try:
                menu.tk_popup(e.x_root, e.y_root)
            finally:
                menu.grab_release()

        tkw.bind("<Button-3>", _popup)

    def _make_text_copyable(self, container):
        """Recursively give every entry, textbox, and message label inside a
        window a mouse-only right-click copy/paste menu. Call once, last, in a
        window builder. Buttons, checkboxes, and other controls are skipped."""
        try:
            if isinstance(container, ctk.CTkEntry):
                self._add_entry_menu(container)
            elif isinstance(container, ctk.CTkTextbox):
                try:
                    editable = str(container.cget("state")) != "disabled"
                except Exception:
                    editable = True
                self._add_textbox_menu(container, editable=editable)
            elif isinstance(container, ctk.CTkLabel):
                try:
                    txt = container.cget("text")
                except Exception:
                    txt = ""
                if txt and txt.strip():
                    self._add_label_copy(container)
        except Exception:
            pass

        try:
            for child in container.winfo_children():
                self._make_text_copyable(child)
        except Exception:
            pass

    def _raise_daemon_error(self, response):
        """[PATCH:error-passthrough] The broker daemon wraps EVERY upstream
        failure as HTTP 500 + {"status":"error","message": <real reason>}.
        raise_for_status() raised on the status code BEFORE reading that body,
        so the GUI's classifier only ever saw the literal string '500':
        a real upstream 429 got the FAST 3s transport retries instead of the
        long capacity backoff, and the on-screen log could never show the true
        cause. This reads the body FIRST and raises the daemon's actual
        message, so _translate_api_error and the is_capacity check finally see
        what really happened upstream (Anthropic or Google alike)."""
        if response.status_code < 400:
            return
        try:
            detail = (response.json() or {}).get("message", "")
        except Exception:
            detail = (response.text or "")[:300]
        # [PATCH:no-500-mask] The daemon wraps EVERY upstream failure as its
        # own HTTP 500, so prefixing the detail with that transport code meant
        # the classifier's "500" branch matched first and the TRUE upstream
        # cause (413 payload cap, 400 bad request, 404 model, auth) was never
        # classified or displayed. Raise the detail alone -- it already carries
        # the real upstream code. Keep our code only when there is no detail.
        raise Exception(detail if detail
                        else f"Daemon HTTP {response.status_code} (no detail)")

    def _translate_api_error(self, raw_error):
        """Translates technical Vertex AI infrastructure messages into concise, non-alarming prose."""
        error_str = str(raw_error).lower()
        
        # [PATCH:client-timeout-coherence] A LOCAL read timeout (the GUI
        # hanging up on 127.0.0.1:5000) carries 'port=5000', whose substring
        # "500" false-matched the 500 branch below and printed a provider
        # outage that never happened (2026-07-08, ID 105). Classify it FIRST.
        if "read timed out" in error_str or "read timeout" in error_str:
            return (
                "The local grading daemon did not reply within the client's wait "
                "window; the grade may still be completing upstream. If this "
                "recurs after the timeout fix, check ledger-daemon.service. "
                "(Local - Client Timeout)"
            )

        # [PATCH:wizard-devapi] The Gemini Developer API answers a bad AI Studio
        # key with HTTP 400 + "API key not valid" (reason API_KEY_INVALID). The
        # generic 400 branch below would call that a document-format problem
        # and retry. Classify it FIRST: it is permanent (see is_permanent in
        # evaluate_grid_with_silo_2) and the real reason belongs on screen.
        if "api key not valid" in error_str or "api_key_invalid" in error_str:
            return (
                "The Google AI Studio key on this machine was rejected by Google "
                "(API_KEY_INVALID). Re-check the key in Google AI Studio, then "
                "ADVANCED -> FACTORY RESET and re-initialize with the correct key. "
                "(Code 400 - Key Rejected)"
            )

        if "resource exhausted" in error_str or "429" in error_str:
            return (
                "High cloud traffic detected. Pacing output and resuming shortly. "
                "(Code 429 - Queue Full)"
            )
            
        elif "503" in error_str or "service unavailable" in error_str:
            return (
                "Temporary cloud congestion. Pausing to allow servers to catch up. "
                "(Code 503 - Server Pacing)"
            )
            
        elif ("413" in error_str or "request_too_large" in error_str
              or "prompt is too long" in error_str):
            return (
                "Request exceeds the provider's payload/context ceiling -- retrying "
                "cannot succeed. Reduce pages per call or image resolution. "
                "(Code 413 - Payload Over Cap)"
            )

        elif "500" in error_str or "internal server error" in error_str:
            return (
                "Brief network interruption. Resubmitting document automatically. "
                "(Code 500 - Connection Paused)"
            )
            
        elif "403" in error_str or "permission denied" in error_str:
            return (
                "The provider rejected the credentials. Check that the API key is valid "
                "and billing is enabled on its project. (Code 403 - Key Rejected)"
            )
            
        elif "400" in error_str or "bad request" in error_str:
            return (
                "Document format is ambiguous. Re-centering for a cleaner pass. "
                "(Code 400 - Format Adjustment)"
            )
            
        else:
            return (
                "Minor network variation detected. Re-attempting connection. "
                "(System - Network Pacing Engaged)"
            )

    def verify_directories(self):
        dirs = [LEDGER_DIR, ENGINE_DIR, DIAGNOSTIC_DIR, MASTER_DIR, COURSES_DIR, UNEVALUATED_DIR, ORIGINAL_SCANS_DIR, TEMP_DIR, SIS_EXPORTS_DIR]
        for period in PERIODS:
            dirs.extend([
                os.path.join(COURSES_DIR, period), 
                os.path.join(COURSES_DIR, period, "Evaluated Assignments")
            ])
        for d in dirs:
            if not os.path.exists(d): 
                os.makedirs(d)
    
    def clean_temp_directory(self):
        """Sweeps the Temp Ledger Files directory clean. [cite: 335]"""
        if os.path.exists(TEMP_DIR):
            for filename in os.listdir(TEMP_DIR):
                file_path = os.path.join(TEMP_DIR, filename)
                try:
                    if os.path.isfile(file_path) or os.path.islink(file_path):
                        os.unlink(file_path)
                    elif os.path.isdir(file_path):
                        shutil.rmtree(file_path)
                except Exception as e:
                    self.log_message(f"[!] Temp Cleanup Failed: {file_path}. Reason: {e}")
        else:
            os.makedirs(TEMP_DIR)

    def update_indicator(self, state):
        if state == "running":
            self.status_label.configure(text="SYSTEM: RUNNING", text_color="#e74c3c")
        else:
            self.status_label.configure(text="SYSTEM: IDLE", text_color="#1e8449")

    def start_ledger(self):
        # [PATCH:no-default-prompt] Empty (or whitespace-only) instructions:
        # refuse before anything starts -- no calibration window, no batch,
        # nothing sent to the daemon. The system never leaves idle.
        if not self.is_running and not self.get_grading_instructions().strip():
            self.status_label.configure(text=NO_INSTRUCTIONS_BANNER, text_color="#c0392b")
            self.log_message(f"[!] {NO_INSTRUCTIONS_LOG}")
            return

        max_score_str = self.max_score_entry.get().strip()
        if not max_score_str.isdigit() or int(max_score_str) <= 0:
            self.log_message("[!] Error: Enter a valid Max Score.")
            return
            
        if not self.master_pdf_path:
            self.log_message("[!] Error: You must load a Master PDF before running Ledger.")
            return

        if self.runtime is not None:
            try:
                configure_tesseract()
                self._xelatex = xelatex_command()
                self.runtime.start()
            except (OSError, RuntimeError) as exc:
                messagebox.showerror("Ledger could not start grading", str(exc), parent=self.root)
                return

        self.global_max_score = int(max_score_str)

        # [PATCH:essay-mode] Read the pipeline mode ONCE per run so a mid-batch
        # tap on the selector can never change strategy under a running job.
        self.grading_mode = self.get_grading_mode()
        self.log_message(f"Pipeline mode for this run: "
                         f"{'ESSAY (FULL-PAGE)' if self.grading_mode == 'essay' else 'WORKSHEET (BOXED)'}.")

        # [PATCH:essay-tag-quiet] The corner-tag OCR exists ONLY to group
        # pages into essays for CHUNKED packing (LEDGER_ESSAY_MAX_PAGES_PER_CALL
        # > 0). In single-call mode (budget 0, the default) every page of a
        # student travels in ONE call and the model groups items from the
        # printed tags itself -- the local OCR result is never consulted.
        # Resolve that ONCE per run: in single-call mode the OCR is skipped
        # entirely, so the teacher never sees per-page "Essay Tag: unreadable"
        # noise about a value the pipeline does not use.
        try:
            self.essay_tag_ocr_needed = int(os.environ.get(
                "LEDGER_ESSAY_MAX_PAGES_PER_CALL", "0")) > 0
        except (TypeError, ValueError):
            self.essay_tag_ocr_needed = False
        if self.grading_mode == "essay" and not self.essay_tag_ocr_needed:
            self.log_message("Essay grouping: single-call mode -- each student's "
                             "pages travel together in one call (corner-tag OCR skipped).")

        if not self.is_running and not self._batch_pending:
            self._batch_model = self.get_silo2_model()
            self.is_running = True
            self.run_btn.configure(state="disabled")
            self.model_btn.configure(state="disabled")
            self.max_score_entry.configure(state="disabled")
            self.mode_selector.configure(state="disabled")
            self.master_btn.configure(state="disabled")
            self.update_indicator("running")
            
            # INTERCEPT: Open Calibration Studio for visual check instead of instant run
            # [PATCH:calib-session-skip] ...unless zones were SAVED in this grader
            # session (either studio); a restart clears that. No scan -> studio,
            # which shows SCAN REQUIRED and resets the run.
            scan_files = [f for f in os.listdir(UNEVALUATED_DIR) if f.lower().endswith('.pdf')]
            if self.calibrated_this_session and scan_files:
                target_pdf = os.path.join(UNEVALUATED_DIR, scan_files[0])
                self.log_message("Calibration already saved this session. Starting Ledger engine...")
                self._start_batch_worker(target_pdf)
            else:
                self.open_calibration_tool(is_intercept=True)

    def _start_batch_worker(self, target_pdf):
        if not self.is_running or self._batch_pending:
            return
        self._batch_pending = True
        threading.Thread(target=self._run_batch_worker, args=(target_pdf,), daemon=True).start()

    def _run_batch_worker(self, target_pdf):
        try:
            self.run_batch_extraction(target_pdf)
        except Exception as exc:
            self.log_message(f"[!] Batch processing stopped. {exc}")
        finally:
            self.finish_processing()

    def shut_down(self):
        self.is_running = False
        if self._batch_pending:
            self.log_message("Stop requested. Waiting for the current operation to finish.")
            return
        self._batch_model = None
        self.model_btn.configure(state="normal")
        self.update_indicator("idle")
        self.run_btn.configure(state="normal")
        self.max_score_entry.configure(state="normal")
        self.mode_selector.configure(state="normal")   # [PATCH:essay-mode]
        self.master_btn.configure(state="normal")
        self.clean_temp_directory()
        self.log_message("Shut Down initiated. Temp folder scrubbed. System is Idle.")

    def image_to_base64_png(self, image_path):
        buffer = io.BytesIO()
        with Image.open(image_path) as img:
            if img.mode != 'RGBA' and img.mode != 'RGB':
                img = img.convert('RGB')
            img.save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode("utf-8")

    # ==========================================
    # SURGERY AREA 1: VERTEX CONTEXT CACHING
    # ==========================================
    def initialize_silo_2_cache(self, master_pdf_path):
        self.log_message("Rendering Master PDF into images for context cache...")
        master_parts_b64 = []
        # [PATCH:silo2-per-chunk-context] keep the rendered master page FILES
        # (and a per-batch b64 cache) so boxed chunks can carry their own pages.
        self.s2ctx_master_files = []
        self._s2ctx_b64_cache = {}
        try:
            doc = fitz.open(master_pdf_path)
            for i, page in enumerate(doc):
                pix = page.get_pixmap(matrix=fitz.Matrix(4.16, 4.16))
                temp_master_path = os.path.join(TEMP_DIR, f"master_page_cache_{i}.png")
                pix.save(temp_master_path)
                self.s2ctx_master_files.append(temp_master_path)
                master_parts_b64.append(self.image_to_base64_png(temp_master_path))
            doc.close()
        except Exception as e:
            self.log_message(f"[!] Failed to render Master PDF images: {e}")
            return False

        self.log_message("Uploading Context Cache via Root Daemon...")
        try:
            live_prompt = self.get_grading_instructions()
            if not live_prompt.strip():
                # Cleared after RUN was pressed (e.g. during calibration).
                self.log_message(f"[!] {NO_INSTRUCTIONS_LOG}")
                return False
            self.log_message(f"-> Grading instructions: {len(live_prompt)} characters.")

            payload = {
                "model_name": self.get_silo2_model(),
                "system_instruction": live_prompt,
                "images_b64": master_parts_b64
            }
            
            if not payload["model_name"].lower().startswith(DESKTOP_MODEL_PREFIXES):
                time.sleep(10) # Network buffer before uploading massive cache payload
            response = self._daemon_request("POST", "/api/cache/build", json=payload, timeout=120)
            self._raise_daemon_error(response)
            
            self.cached_silo2_content = True # Just a local flag now
            self.log_message("-> Context Cache successfully established! Valid for 12 hours.")
            # [PATCH:silo2-per-chunk-context] Dashboard status + daemon capability
            # gate. An older daemon would silently IGNORE master_images_b64 (grades
            # still correct, but full-weight) -- gate it OFF loudly here instead,
            # so an A/B arm can never be silently invalid.
            if os.environ.get("LEDGER_S2CTX_ENABLE", "1") != "1":
                self.log_message("-> Per-chunk master context: DISABLED (LEDGER_S2CTX_ENABLE=0) - every chunk carries the FULL master (A-arm).")
                self.s2ctx_master_files = []
            else:
                try:
                    _health = self._daemon_request("GET", "/", timeout=10).json()
                except Exception:
                    _health = {}
                if _health.get("per_chunk_context"):
                    self.log_message("-> Per-chunk master context: ACTIVE - each boxed chunk carries only its own master page(s) plus the standing include page(s); unreadable footers fall back to the FULL master.")
                else:
                    self.log_message("[!] Per-chunk master context: this daemon predates it (need v2.6) - every chunk carries the FULL master. Grades unaffected; weight reduction inactive.")
                    self.s2ctx_master_files = []
            return True
        except Exception as e:
            self.log_message(f"[!] Failed to build Google context cache via Daemon: {e}")
            return False

    def delete_cache(self):
        try:
            self._daemon_request("POST", "/api/cache/delete", timeout=15)
            self.cached_silo2_content = False
            self.log_message("-> Google context cache successfully purged.")
        except Exception as e:
            self.log_message(f"[!] Failed to delete cache via Daemon: {e}")
     
    # ==========================================
    # SURGERY AREA 2: OPENCV BOX EXTRACTION (GEOMETRY UPGRADE)
    # ==========================================
    def extract_boxes_with_opencv(self, full_page_img_path, page_num):
        """Finds LibreOffice boxes using adaptive thresholding, strict geometry, and extent."""
        snippets = []
        try:
            # 1. Read image in grayscale
            img = _read_cv_image(full_page_img_path)
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            
            # 2. Adaptive Thresholding: Dynamically handles scanner shadows/gradients
            thresh = cv2.adaptiveThreshold(
                gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, 
                cv2.THRESH_BINARY_INV, 51, 15
            )
            
            # 3. Morphological Close: Leverages your 0.04" line thickness to fuse 
            # borders into unbroken solid shapes, making them mathematically perfect.
            kernel = np.ones((5,5), np.uint8)
            closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)
            
            # 4. Find Contours
            contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            
            # 5. The Geometry & Extent Filter
            valid_boxes = []
            for cnt in contours:
                area = cv2.contourArea(cnt)
                
                # Filter 1: Dust filter (No maximum size limit)
                if area > 10000:
                    perimeter = cv2.arcLength(cnt, True)
                    # Smoothed from 0.02 to 0.03 to ignore minor handwriting bumps
                    approx = cv2.approxPolyDP(cnt, 0.03 * perimeter, True)
                    
                   # Filter 2: Allow up to 15 corners to accommodate handwriting bumps/tails.
                    if 4 <= len(approx) <= 15:
                        x, y, w, h = cv2.boundingRect(approx)
                        rect_area = w * h
                        
                        # Filter 3: The Height Check (The Paragraph Assassin)
                        # Rejects fused text strings and rogue equations (usually < 50px tall)
                        if h > 75:
                            
                            # Filter 4: The Extent Check
                            # Lowered to 0.65 to allow students' handwriting to cross box boundaries.
                            extent = float(area) / rect_area
                            if extent > 0.65:
                                valid_boxes.append((x, y, w, h))
            
            # 6. Spatial Sorting Math (Top-to-Bottom, Left-to-Right)
            def get_sort_key(box):
                x, y, w, h = box
                return (y // 150, x)
            
            valid_boxes.sort(key=get_sort_key)
            
            # 7. Crop and Save
            box_count = 0
            for x, y, w, h in valid_boxes:
                cropped = img[y:y+h, x:x+w]
                snippet_path = os.path.join(TEMP_DIR, f"snippet_p{page_num}_b{box_count}.png")
                _write_cv_image(snippet_path, cropped)
                snippets.append(snippet_path)
                box_count += 1
            
            self.log_message(f"    -> OpenCV isolated and sorted {box_count} perfect rectangles on page {page_num}.")
            return snippets
            
        except Exception as e:
            self.log_message(f"[!] OpenCV Error on {full_page_img_path}: {e}")
            return []
    
    # ==========================================
    # SURGERY AREA 3: THE LOCAL FLASHCARD STITCH (UPGRADED TO 2D GRID)
    # ==========================================
    def _stitch_paths_to_image(self, snippet_paths):
        """[PATCH:silo2-chunking] Row-packs a list of snippet image paths into a
        single PIL Image (balanced 2D grid, width-thresholded). Returns the
        canvas, or None if empty. Does not save to disk."""
        if not snippet_paths:
            return None
        images = [Image.open(p) for p in snippet_paths]

        MAX_ROW_WIDTH = 1600
        PADDING = 15

        rows = []
        current_row = []
        current_row_width = 0
        current_row_height = 0

        for img in images:
            if current_row_width + img.width + PADDING > MAX_ROW_WIDTH and current_row:
                rows.append({"images": current_row,
                             "width": current_row_width - PADDING,
                             "height": current_row_height})
                current_row = []
                current_row_width = 0
                current_row_height = 0
            current_row.append(img)
            current_row_width += img.width + PADDING
            current_row_height = max(current_row_height, img.height)

        if current_row:
            rows.append({"images": current_row,
                         "width": current_row_width - PADDING,
                         "height": current_row_height})

        if not rows:
            return None

        total_width = max(row["width"] for row in rows)
        total_height = sum(row["height"] for row in rows) + (PADDING * (len(rows) - 1))

        canvas = Image.new('RGB', (total_width, total_height), color='white')
        current_y = 0
        for row in rows:
            current_x = 0
            for img in row["images"]:
                canvas.paste(img, (current_x, current_y))
                current_x += img.width + PADDING
            current_y += row["height"] + PADDING
        return canvas

    def stitch_snippets_to_grid(self, snippet_paths, b_id, master_page_map=None):
        """[PATCH:silo2-chunking] Returns a LIST of chunk descriptors (was: a
        list of bare paths). Chunks on PAGE boundaries -- the page number is
        parsed from each snippet filename ('snippet_p<page>_b<n>.png') -- and
        NEVER splits a page, so multi-box / cross-referenced items on a page
        stay together. Whole page grids are packed vertically until
        LEDGER_SILO2_MAX_CHUNK_PX, then a new chunk starts; a single page
        taller than the budget becomes its own chunk.

        [PATCH:silo2-per-chunk-context] Each descriptor is a dict:
            {"path": <chunk png>, "footer_pages": [printed page nums] | None}
        footer_pages comes from master_page_map (snippet path -> the FOOTER
        page number OCR'd in Pass 1). The p<page> in the snippet filename is
        the SCAN-STACK index and is used ONLY for stitch geometry -- it is
        never used to pick master pages, because a scan stack is multi-student
        and order-independent. Any page whose footer read is missing or 999
        makes the whole chunk's footer_pages None -> FULL-master fallback
        (an OCR miss may cost weight, never correctness).
        """
        if not snippet_paths:
            return []
        try:
            MAX_CHUNK_HEIGHT = int(os.environ.get("LEDGER_SILO2_MAX_CHUNK_PX", "3500"))
            PADDING = 15

            # 1. Group snippets by scan page, preserving first-seen order, and
            #    collect each scan page's footer-derived master page number.
            pages = {}
            page_order = []
            page_footers = {}
            for p in snippet_paths:
                m = re.search(r'snippet_p(\d+)_b\d+', os.path.basename(p))
                pg = int(m.group(1)) if m else 0
                if pg not in pages:
                    pages[pg] = []
                    page_order.append(pg)
                    page_footers[pg] = set()
                pages[pg].append(p)
                page_footers[pg].add(master_page_map.get(p) if master_page_map else None)

            # 2. Stitch each page into its own grid image (true heights).
            page_imgs = []
            for pg in page_order:
                canvas = self._stitch_paths_to_image(pages[pg])
                if canvas is not None:
                    page_imgs.append((canvas, page_footers[pg]))
            if not page_imgs:
                return []

            # 3. Greedily pack whole page grids into vertical chunks under budget.
            chunks = []
            cur = []
            cur_f = set()
            cur_h = 0
            for img, fset in page_imgs:
                add = img.height + (PADDING if cur else 0)
                if cur and (cur_h + add) > MAX_CHUNK_HEIGHT:
                    chunks.append((cur, cur_f))
                    cur = []
                    cur_f = set()
                    cur_h = 0
                    add = img.height
                cur.append(img)
                cur_f = cur_f | fset
                cur_h += add
            if cur:
                chunks.append((cur, cur_f))

            # 4. Render + save each chunk; resolve its footer page set.
            chunk_descs = []
            for i, (imgs, fset) in enumerate(chunks):
                w = max(im.width for im in imgs)
                h = sum(im.height for im in imgs) + PADDING * (len(imgs) - 1)
                canvas = Image.new('RGB', (w, h), color='white')
                y = 0
                for im in imgs:
                    canvas.paste(im, (0, y))
                    y += im.height + PADDING
                out_path = os.path.join(TEMP_DIR, f"stitched_grid_{b_id}_chunk{i + 1}.png")
                canvas.save(out_path)
                footer_pages = None
                if fset and all(isinstance(f, int) and 0 < f < 999 for f in fset):
                    footer_pages = sorted(fset)
                chunk_descs.append({"path": out_path, "footer_pages": footer_pages})

            self.log_message(f"    -> Stitched {len(snippet_paths)} snippets across {len(page_imgs)} page(s) "
                             f"into {len(chunk_descs)} chunk(s) (page-aligned, <= {MAX_CHUNK_HEIGHT}px tall).")
            # [PATCH:silo2-per-chunk-context] Dashboard: the context each chunk
            # will carry, decided here from the footer reads.
            for i, ch in enumerate(chunk_descs, 1):
                if ch["footer_pages"] is not None:
                    self.log_message(f"       chunk {i}: master page(s) {ch['footer_pages']} (footer-derived)")
                else:
                    self.log_message(f"       chunk {i}: footer page number unreadable -> FULL-master context for this chunk")
            return chunk_descs

        except Exception as e:
            self.log_message(f"[!] Stitching Error for {b_id}: {e}")
            return []

    # ==========================================
    # [PATCH:essay-mode] FULL_PAGE PIPELINE HELPERS
    # ==========================================
    def _ocr_essay_tag(self, page_img_path):
        """Locally OCRs the printed corner tag (white item number in a dark
        box, top-left of the response box) so pages can be grouped into
        essays WITHOUT an API call. Offline, like the footer page-number OCR.

        [PATCH:essay-tag-v2] The tag square sits TOUCHING the response-box
        border, so a plain threshold fuses tag + border into one giant blob
        and 'largest dark contour' selects the box corner, not the tag (the
        v1 failure: every page 'unreadable'). v2 therefore (a) applies a
        morphological OPEN wider than the border stroke, which erases thin
        border lines while the solid tag square survives, then (b) selects
        by tag GEOMETRY -- solid (extent), square-ish (aspect), sized -- and
        scores the best candidate rather than the biggest blob.

        Tunables (environment):
          LEDGER_ESSAY_TAG_REGION   x,y,w,h page fractions searched
                                    (default 0.00,0.05,0.30,0.22)
          LEDGER_ESSAY_TAG_OPEN_PX  opening kernel; must exceed the printed
                                    border stroke width in pixels
                                    (default 15; border is ~12 px at 300 DPI)
          LEDGER_ESSAY_TAG_DEBUG    '1' saves the OCR'd crop to TEMP_DIR as
                                    tagdebug_<page>.png for field diagnosis
        Returns an int, or None when no tag-like candidate reads as digits."""
        try:
            frac = os.environ.get("LEDGER_ESSAY_TAG_REGION", "0.00,0.05,0.30,0.22")
            fx, fy, fw, fh = [float(v) for v in frac.split(",")]
            img = _read_cv_image(page_img_path)
            H, W = img.shape[:2]
            x0, y0 = int(W * fx), int(H * fy)
            x1, y1 = min(W, x0 + int(W * fw)), min(H, y0 + int(H * fh))
            region = cv2.cvtColor(img[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)

            _, thresh = cv2.threshold(region, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

            # (a) First CLOSE to fill the white digit's slits so the tag is a
            # solid square (otherwise the digit strokes can fragment it under
            # erosion), then OPEN wider than the border stroke: thin lines
            # (box border, ruled lines, most text) are erased while the
            # solid tag square survives erosion and is restored by dilation.
            open_px = int(os.environ.get("LEDGER_ESSAY_TAG_OPEN_PX", "15"))
            close_px = max(3, open_px // 2)
            solid = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE,
                                     np.ones((close_px, close_px), np.uint8))
            kernel = np.ones((open_px, open_px), np.uint8)
            opened = cv2.morphologyEx(solid, cv2.MORPH_OPEN, kernel)

            contours, _ = cv2.findContours(opened, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            # (b) Geometry gate: the tag is a SOLID, roughly SQUARE mark of
            # modest size. Border remnants are long/thin (aspect/extent fail);
            # page-scale structures are too big (area cap).
            best = None
            best_score = 0.0
            region_area = float(region.shape[0] * region.shape[1])
            for c in contours:
                area = cv2.contourArea(c)
                if area < 500 or area > 0.10 * region_area:
                    continue
                x, y, w, h = cv2.boundingRect(c)
                if w == 0 or h == 0:
                    continue
                aspect = w / float(h)
                if not (0.4 <= aspect <= 2.5):
                    continue
                extent = area / float(w * h)
                if extent < 0.55:
                    continue
                score = extent * area
                if score > best_score:
                    best_score = score
                    best = (x, y, w, h)
            if best is None:
                return None

            x, y, w, h = best
            # Re-crop the ORIGINAL grayscale (pre-opening), INSET a few px
            # INSIDE the tag square: sampling past the square's edge pulls in
            # a ring of white page that inverts into a black frame around the
            # digit, and tesseract reads frame edges as strokes (a '1' beside
            # a frame line reads as '4'). Strictly-inside sampling inverts to
            # a clean dark digit on a light field.
            inset = 6
            ry0, ry1 = y + inset, y + h - inset
            rx0, rx1 = x + inset, x + w - inset
            if (ry1 - ry0) < 10 or (rx1 - rx0) < 10:
                ry0, ry1, rx0, rx1 = y, y + h, x, x + w
            roi = region[ry0:ry1, rx0:rx1]
            roi_inv = cv2.bitwise_not(roi)
            # Tesseract is unreliable on tiny lone glyphs (a printed '1' can
            # read as '4'); upscale the crop 4x so the digit presents at a
            # comfortable size, and pad with a white margin.
            roi_inv = cv2.resize(roi_inv, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
            # Binarize so faded/half-toner print presents crisp strokes
            # instead of gray smears (an open-bowled '3' smears into an '8').
            _, roi_inv = cv2.threshold(roi_inv, 0, 255,
                                       cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            roi_inv = cv2.copyMakeBorder(roi_inv, 20, 20, 20, 20,
                                         cv2.BORDER_CONSTANT, value=255)
            if os.environ.get("LEDGER_ESSAY_TAG_DEBUG", "0") == "1":
                dbg = os.path.join(TEMP_DIR, f"tagdebug_{os.path.basename(page_img_path)}")
                _write_cv_image(dbg, roi_inv)
            text = pytesseract.image_to_string(
                roi_inv, config='--psm 8 -c tessedit_char_whitelist=0123456789').strip()
            return int(text) if text else None
        except Exception as e:
            self.log_message(f"    -> [!] Essay tag OCR failed on {os.path.basename(page_img_path)}: {e}")
            return None

    def _resize_page_for_essay(self, page_img_path, b_id, seq):
        """Downscales one response page to the provider ingest ceiling
        (LEDGER_ESSAY_PAGE_MAX_EDGE, default 1568 px on the long edge --
        providers resize past this anyway, so shipping more is pure payload).
        Returns the temp path of the resized copy; pages already under the
        ceiling are copied through untouched."""
        max_edge = int(os.environ.get("LEDGER_ESSAY_PAGE_MAX_EDGE", "1568"))
        out_path = os.path.join(TEMP_DIR, f"essay_page_{b_id}_{seq:02d}.png")
        with Image.open(page_img_path) as img:
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            long_edge = max(img.width, img.height)
            if max_edge > 0 and long_edge > max_edge:
                scale = max_edge / float(long_edge)
                img = img.resize((int(img.width * scale), int(img.height * scale)),
                                 Image.Resampling.LANCZOS)
            img.save(out_path)
        return out_path

    def assemble_essay_pages(self, full_pages, b_id):
        """[PATCH:essay-mode] The FULL_PAGE counterpart of
        stitch_snippets_to_grid. Takes the student's full_pages records
        (path + footer page number + locally-OCR'd essay tag), and returns a
        LIST of chunks where each chunk is a LIST of resized page paths --
        the shape evaluate_chunks_with_silo_2 already iterates, and the shape
        evaluate_grid_with_silo_2 turns into an images_b64 payload.

        Grouping rules:
          * Pages are ordered by footer page number (scan-order independent).
          * Consecutive pages sharing an essay tag are ONE essay group; a
            page whose tag could not be read INHERITS the previous page's
            essay (the continuation heuristic -- a second essay page is the
            most likely reason a fresh tag is absent/unreadable), and is
            logged so the teacher can verify.
          * Chunks are packed from WHOLE essay groups, never splitting a
            group, up to LEDGER_ESSAY_MAX_PAGES_PER_CALL pages per call
            (0 = unlimited: the entire packet travels as one call, the
            preferred setting on the Anthropic route)."""
        if not full_pages:
            return []
        try:
            max_pages = int(os.environ.get("LEDGER_ESSAY_MAX_PAGES_PER_CALL", "0"))

            ordered = sorted(full_pages, key=lambda p: p.get("lowest_q", 999))

            # [PATCH:essay-mode] A page whose footer number failed local OCR
            # carries the 999 sentinel and sorts LAST. In single-call mode
            # (budget 0) that is harmless -- the model reads the printed tags
            # and regroups. In CHUNKED mode a misplaced page can land in the
            # wrong call, separated from its essay, so say so loudly.
            unnumbered = [p for p in ordered if p.get("lowest_q", 999) >= 999]
            if unnumbered and max_pages > 0:
                self.log_message(
                    f"    -> [!] WARNING (ID {b_id}): {len(unnumbered)} page(s) have no "
                    f"readable footer page number and will sort LAST. With a per-call "
                    f"page budget active, a misplaced page can be graded apart from its "
                    f"essay. Verify this student's conspectus, or re-run with "
                    f"LEDGER_ESSAY_MAX_PAGES_PER_CALL=0.")

            # 1. Group pages into essays via the corner tag + continuation rule.
            groups = []          # list of lists of page dicts
            last_tag = None
            for pg in ordered:
                tag = pg.get("essay_num")
                if tag is None and groups:
                    # [PATCH:essay-tag-quiet] The continuation heuristic only
                    # steers CHUNKED packing; in single-call mode grouping is
                    # moot (one chunk regardless), so say nothing.
                    if max_pages > 0:
                        self.log_message(
                            f"    -> Page {pg.get('lowest_q', '?')} (ID {b_id}): no readable "
                            f"essay tag; treating as a CONTINUATION of essay {last_tag}.")
                    groups[-1].append(pg)
                    continue
                if tag is not None and groups and tag == last_tag:
                    groups[-1].append(pg)
                    continue
                groups.append([pg])
                last_tag = tag

            # 2. Resize every page once, preserving order.
            seq = 0
            sized_groups = []
            for g in groups:
                sized = []
                for pg in g:
                    seq += 1
                    sized.append(self._resize_page_for_essay(pg["path"], b_id, seq))
                sized_groups.append(sized)

            # 3. Pack whole essay groups into chunks under the page budget.
            chunks = []
            cur = []
            for g in sized_groups:
                if max_pages > 0 and cur and (len(cur) + len(g)) > max_pages:
                    chunks.append(cur)
                    cur = []
                cur.extend(g)
            if cur:
                chunks.append(cur)

            self.log_message(
                f"    -> Assembled {len(ordered)} page(s) into {len(groups)} essay group(s), "
                f"{len(chunks)} Silo 2 call(s) (essay-aligned"
                f"{', <= ' + str(max_pages) + ' pages/call' if max_pages > 0 else ', single call'}).")
            return chunks

        except Exception as e:
            self.log_message(f"[!] Essay assembly error for {b_id}: {e}")
            return []

    def extract_with_silo_1(self, header_path, footer_path):
        # --- LOCAL OFFLINE OCR (FOOTER / PAGE NUMBER) ---
        # [PATCH:boxed-digit-ocr] was: boundingRect + bitwise_not, which
        # inverted the box rim into ink beside the digits (6/25 correct on
        # the Algebra 1 EOC sample; two silent misreads, 200 and 307).
        page_num = 999
        try:
            footer_img = _read_cv_image(footer_path)
            gray_f = cv2.cvtColor(footer_img, cv2.COLOR_BGR2GRAY)
            _, thresh_f = cv2.threshold(gray_f, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
            contours, _ = cv2.findContours(thresh_f, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            if contours:
                c = max(contours, key=cv2.contourArea)
                if cv2.contourArea(c) > 500:
                    hi = int(os.environ.get("LEDGER_FOOTER_MAX_PAGE", "99"))
                    val, raw = _read_boxed_digits(gray_f, c, thresh_f, lo=1, hi=hi)
                    if val is None:
                        # A corrupt read is now REJECTED, not silently cast.
                        # int("307") used to sail straight through.
                        self.log_message(
                            f"    -> [!] Footer OCR rejected {raw!r} on "
                            f"{os.path.basename(footer_path)} (page -> 999)")
                    else:
                        page_num = val
        except Exception as e:
            self.log_message(f"    -> [!] Local Page Number OCR Failed: {e}")

        # --- PROVIDER OCR (BARTONIAN ID ONLY) ---
        delay = 15
        model_name = self.get_silo2_model()
        use_desktop = model_name.lower().startswith(DESKTOP_MODEL_PREFIXES)
        while self.is_running:
            try:
                # The image is already perfectly cropped by the dynamic coordinates
                base64_header = self.image_to_base64_png(header_path)
                
                prompt = (
                    "You are receiving a cropped image showing the top-right corner of an assignment containing a Bartonian ID.\n"
                    "Return ONLY a valid, flat JSON object using EXACTLY this template: "
                    '{"bartonian_id": 125} \n\n'
                    "CRITICAL OCR RULES FOR BARTONIAN ID:\n"
                    "1. The Bartonian ID is always a continuous integer. Output it as a single integer (0 if missing).\n"
                    "2. VISION HEURISTIC: Students often write the digit '1' as a simple vertical stroke. "
                    "Therefore, any character in the ID field that resembles a vertical bar (|), a slash (/), "
                    "the letter 'l', or the letter 'I' MUST be interpreted as an integer.\n"
                    "3. FUSION MANDATE: Do not treat vertical marks as delimiters or separators. You must fuse "
                    "them into the resulting integer. If you see '1 | 25', '| 25', or '1 25', the "
                    "output MUST be 125. Never discard a leading or middle vertical stroke.\n"
                    "4. Strip any non-numeric symbols that are clearly not digits.\n"
                    "5. Bartonian IDs are always three-digit numbers between 101-799."
                )
                
                payload = {
                    "model_name": model_name,
                    "prompt": prompt,
                    "image_b64": base64_header
                }
                
                timeout = DESKTOP_OCR_TIMEOUT if use_desktop else 60
                response = self._daemon_request("POST", "/api/silo1", json=payload, timeout=timeout)
                self._raise_daemon_error(response)
                data = response.json()
                
                if data.get("status") == "success":
                    if use_desktop:
                        silo_data = json.loads(data['text'])
                    else:
                        raw = data["text"].strip().replace('```json', '').replace('```', '').strip()
                        silo_data = json_repair.loads(raw)
                    if use_desktop and (not isinstance(silo_data, dict) or type(silo_data.get("bartonian_id")) is not int):
                        raise DesktopOcrError("The desktop provider returned an invalid ID transcription.")
                    silo_data["page_num"] = page_num
                    return silo_data
                else:
                    raise Exception(data.get("message", "Daemon returned an error."))
                
            except Exception as e:
                if use_desktop:
                    raise DesktopOcrError(str(e)) from None
                logging.info(f"[!] Silo 1 Error. Retrying in {delay} seconds... Details: {e}")
                friendly_err = self._translate_api_error(e)
                self.root.after(0, self._append_to_gui, f"Silo 1 Status: {friendly_err} (Retrying in {delay}s)")
                time.sleep(delay)
                delay = min(delay + 15, 300)
        return None
   
    def _s2ctx_master_images_b64(self, footer_pages):
        """[PATCH:silo2-per-chunk-context] Resolve a chunk's footer-derived
        page numbers into base64 payloads of the locally rendered master
        pages. Returns a list for the daemon's 'master_images_b64' key, or
        None -> the caller omits the key and the daemon grades against the
        FULL cached master exactly as before (fail-safe: an unreadable or
        implausible footer may cost weight, never correctness).

        Env knobs (GUI-side -- LAUNCHER environment ONLY, per the Rev-fff
        placement split; a value in /etc/paragon/.env silently does nothing):
          LEDGER_S2CTX_ENABLE        "1" (default) | "0" = full-master A-arm
          LEDGER_S2CTX_PAGE_OFFSET   int, default "0": master idx = footer-1+offset
          LEDGER_S2CTX_ADJACENCY     int, default "0": attach +/- N neighbors
          LEDGER_S2CTX_INCLUDE_PAGES default "1": comma list of 1-based master
                                     pages ALWAYS attached (cover/instructions
                                     -- adjacency policy (c), Rev hhh / #79)
        """
        try:
            if os.environ.get("LEDGER_S2CTX_ENABLE", "1") != "1":
                return None
            masters = getattr(self, "s2ctx_master_files", None)
            if not masters or not footer_pages:
                return None
            n = len(masters)
            offset = int(os.environ.get("LEDGER_S2CTX_PAGE_OFFSET", "0"))
            radius = int(os.environ.get("LEDGER_S2CTX_ADJACENCY", "0"))
            include = os.environ.get("LEDGER_S2CTX_INCLUDE_PAGES", "1")
            idxs = set()
            for fp in footer_pages:
                base = int(fp) - 1 + offset
                if base < 0 or base >= n:
                    self.log_message(f"    -> [S2CTX] footer page {fp} resolves outside the "
                                     f"{n}-page master -> FULL-master context for this chunk.")
                    return None
                for d in range(-radius, radius + 1):
                    j = base + d
                    if 0 <= j < n:
                        idxs.add(j)
            for tok in include.split(","):
                tok = tok.strip()
                if tok:
                    j = int(tok) - 1
                    if 0 <= j < n:
                        idxs.add(j)
            if not idxs:
                return None
            # [PATCH:silo2-per-chunk-context v2] SMALL-FORM SHORT-CIRCUIT: if the
            # resolved set covers the WHOLE master (the normal case for 2-3 page
            # worksheets once the include page is added), inlining it per call is
            # strictly worse than the cached path -- identical weight, no cache
            # discount. Return None -> the daemon grades against the full cached
            # master, byte-path identical to the pre-#79 pipeline. Per-chunk
            # context therefore engages only where it pays: multi-page masters
            # whose chunks cover a SUBSET of the pages.
            if len(idxs) >= n:
                return None
            cache = getattr(self, "_s2ctx_b64_cache", None)
            if cache is None:
                cache = self._s2ctx_b64_cache = {}
            out = []
            for j in sorted(idxs):
                if j not in cache:
                    cache[j] = self.image_to_base64_png(masters[j])
                out.append(cache[j])
            return out
        except Exception as e:
            self.log_message(f"    -> [S2CTX] context resolution failed ({e}) -> FULL-master context for this chunk.")
            return None

    def evaluate_chunks_with_silo_2(self, chunk_paths):
        """[PATCH:silo2-chunking] Grades each grid chunk via Silo 2 and merges the
        JSON arrays into one evaluation. Returns the merged list, or None if ANY
        chunk defers (capacity/Stop) -- a partial grade would silently under-score
        a student, so one deferred chunk defers the whole student (scan retained,
        re-run recovers). A small inter-chunk pace avoids self-inflicted 429s."""
        if not chunk_paths:
            return []
        use_desktop = self.get_silo2_model().lower().startswith(DESKTOP_MODEL_PREFIXES)
        pace = 0 if use_desktop else float(os.environ.get("LEDGER_SILO2_CHUNK_PACING_S", "60"))
        merged = []
        total = len(chunk_paths)
        for idx, cp in enumerate(chunk_paths, 1):
            if not self.is_running:
                return None
            # [PATCH:silo2-per-chunk-context] Dashboard mirrors the context this
            # chunk will carry: its own master page(s), or the FULL master when a
            # footer read was unreadable (fail-safe) or the feature is off.
            ctx_label = ""
            if isinstance(cp, dict):
                _fp = cp.get("footer_pages")
                if _fp and os.environ.get("LEDGER_S2CTX_ENABLE", "1") == "1" and getattr(self, "s2ctx_master_files", None):
                    ctx_label = f" [master pp. {','.join(str(x) for x in _fp)} + incl.]"
                else:
                    ctx_label = " [FULL master]"
            if total > 1:
                self.root.after(0, self._append_to_gui, f"Silo 2: grading chunk {idx} of {total}{ctx_label}...")
            if idx > 1 and pace > 0:
                # [PATCH:silo2-pacing-60] Sleep in 1s slices so the Stop button
                # stays responsive (same convention as the retry loop below).
                # Stop during pacing defers the student, per contract.
                slept = 0.0
                while slept < pace and self.is_running:
                    time.sleep(min(1.0, pace - slept))
                    slept += 1.0
                if not self.is_running:
                    return None
            result = self.evaluate_grid_with_silo_2(cp)
            if result is None or not self.is_running:
                return None  # capacity-deferred or Stop -> defer the whole student
            if isinstance(result, list):
                merged.extend(result)
        return merged

    def evaluate_grid_with_silo_2(self, grid_image_path):
        """Sends ONLY the stitched grid. Context is pulled from daemon cache.

        [PATCH:silo2-bounded-retry] Bounded-retry / defer-and-continue contract:
          * returns a list  -> a real evaluation (possibly empty if the model
                               genuinely returned no items).
          * returns None    -> bounded retries were exhausted against Google
                               capacity (HTTP 429 / RESOURCE_EXHAUSTED / 503),
                               OR the user pressed Stop. The CALLER must treat
                               None as 'deferred': do NOT write a grade and do
                               NOT build a conspectus.

        A 429 returns fast and healthy, so the daemon watchdog never fires on
        it; the only thing that can turn a transient 429 into an unbounded hang
        is an unbounded retry loop. This loop is therefore bounded, has full
        jitter (so multiple field units do not resync into a thundering herd),
        and backs off long on capacity (429/503) but fast on transport (5xx).
        """
        # [PATCH:silo2-per-chunk-context] Boxed chunks now arrive as dicts
        # ({path, footer_pages}); essay mode still passes a LIST of page paths
        # and legacy callers a bare path -- both byte-path identical to before.
        master_ctx_b64 = None
        if isinstance(grid_image_path, dict):
            master_ctx_b64 = self._s2ctx_master_images_b64(grid_image_path.get("footer_pages"))
            grid_image_path = grid_image_path["path"]
        use_copilot = self.get_silo2_model().lower().startswith("copilot/")
        # A timeout can happen after the desktop app has accepted the request.
        MAX_ATTEMPTS = 1 if use_copilot else int(os.environ.get("LEDGER_SILO2_MAX_ATTEMPTS", "10"))
        BASE_BACKOFF = 15.0   # seconds; capacity (429/503) exponential base
        MAX_BACKOFF  = 300.0  # seconds; ceiling on any single wait
        FAST_BACKOFF = 5.0    # seconds; transport/5xx is not capacity -> retry fast

        attempt = 0
        while self.is_running and attempt < MAX_ATTEMPTS:
            attempt += 1
            try:
                # [PATCH:essay-mode] grid_image_path is a single path (boxed
                # grid -> legacy image_b64 payload) or a LIST of page paths
                # (essay mode -> images_b64 payload, daemon v2.3+). Legacy
                # callers are byte-path identical.
                if isinstance(grid_image_path, (list, tuple)):
                    image_payload = {"images_b64": [self.image_to_base64_png(p) for p in grid_image_path]}
                else:
                    image_payload = {"image_b64": self.image_to_base64_png(grid_image_path)}

                evaluation_schema = {
                    "type": "ARRAY",
                    "items": {
                        "type": "OBJECT",
                        "properties": {
                            "item_number": {"type": "STRING"},
                            "student_answer": {"type": "STRING"},
                            "status": {"type": "STRING"},
                            "feedback": {"type": "STRING"},
                            "points_earned": {"type": "NUMBER"}
                        },
                        "required": ["item_number", "student_answer", "status", "feedback", "points_earned"]
                    }
                }

                payload = {
                    "prompt":"Evaluate this grid of answers based strictly on your cached instructions.",
                    "schema": evaluation_schema
                }
                payload.update(image_payload)
                # [PATCH:silo2-per-chunk-context] attach this chunk's own master
                # page(s); when the key is absent the daemon grades against the
                # FULL cached master exactly as before.
                if master_ctx_b64:
                    payload["master_images_b64"] = master_ctx_b64

                read_timeout = float(os.environ.get("LEDGER_SILO2_CLIENT_TIMEOUT_S", "330"))
                if use_copilot:
                    read_timeout = max(read_timeout, DESKTOP_OCR_TIMEOUT)
                response = self._daemon_request("POST", "/api/silo2", json=payload, timeout=(10, read_timeout))
                if not self.is_running:
                    return None
                self._raise_daemon_error(response)
                data = response.json()

                if data.get("status") == "success":
                    if use_copilot:
                        parsed = json.loads(data['text'])
                    else:
                        raw = data["text"].strip().replace('```json', '').replace('```', '').strip()
                        parsed = json_repair.loads(raw)
                    # [PATCH:literal-newline-normalize] Some models (first seen:
                    # claude-opus-4-8) obey the prompt's backslash-doubling rule so
                    # literally that paragraph breaks arrive as the LITERAL 4-char
                    # sequence \n\n instead of a real blank line. Downstream, the
                    # paragraph splitter (re.split on real blank lines) then finds
                    # no paragraphs, and TeX tokenizes '\n\nThe' as undefined
                    # control words -- eating the break AND the first word of the
                    # next paragraph. Normalize ONLY the doubled sequence here, at
                    # the single parse choke point, so the DB, Tabula, and report
                    # all store clean text. Real newlines (Gemini's output) contain
                    # no literal \n\n, so this is a byte-level no-op on the Google
                    # route; no legitimate LaTeX in the loaded packages can produce
                    # the sequence either (no macro named \n exists).
                    if isinstance(parsed, list):
                        for _it in parsed:
                            if isinstance(_it, dict) and isinstance(_it.get("student_answer"), str):
                                _it["student_answer"] = _it["student_answer"].replace("\\n\\n", "\n\n")
                    return parsed
                else:
                    raise Exception(data.get("message"))

            except Exception as e:
                err_str = str(e).lower()
                is_capacity = ("resource exhausted" in err_str or "429" in err_str
                               or "503" in err_str or "service unavailable" in err_str)

                # [PATCH:permanent-error-fast-defer] Deterministic 4xx failures
                # (payload over the provider cap, malformed request, unknown
                # model, auth) return the SAME answer on every retry. Burning
                # six attempts on them wasted ~2 min/student, and the old
                # hardcoded deferral text blamed "Google capacity (429)" for
                # errors that were neither Google nor capacity. Defer at once
                # and echo the REAL reason into the on-screen log.
                is_permanent = (not is_capacity and
                                ("api key not valid" in err_str          # [PATCH:wizard-devapi]
                                 or "api_key_invalid" in err_str
                                 or "413" in err_str or "request_too_large" in err_str
                                 or "prompt is too long" in err_str
                                 or "invalid_request" in err_str
                                 or "not_found" in err_str
                                 or "permission denied" in err_str
                                 or "authentication" in err_str
                                 or "400" in err_str or "404" in err_str
                                 or "401" in err_str or "403" in err_str))

                # Retries used up, or provably unretriable -> DEFER (return None).
                if attempt >= MAX_ATTEMPTS or is_permanent:
                    logging.info(f"[!] Silo 2 deferring student (attempt {attempt}/{MAX_ATTEMPTS}, permanent={is_permanent}). Details: {e}")
                    reason = ("unretriable provider error" if is_permanent
                              else f"error persisted after {MAX_ATTEMPTS} tries")
                    self.root.after(0, self._append_to_gui,
                                    f"Silo 2 Status: {reason} - student deferred for re-run.")
                    self.root.after(0, self._append_to_gui,
                                    f"Silo 2 Detail: {str(e)[:300]}")
                    return None

                # Capacity backs off exponentially (long); transport retries fast.
                # Full jitter on the low side keeps most of the intended backoff
                # while de-correlating simultaneous units.
                if is_capacity:
                    wait = min(BASE_BACKOFF * (2 ** (attempt - 1)), MAX_BACKOFF)
                else:
                    wait = FAST_BACKOFF
                wait = random.uniform(wait * 0.5, wait)

                logging.info(f"[!] Silo 2 Error (attempt {attempt}/{MAX_ATTEMPTS}, retry in {wait:.0f}s). Details: {e}")
                friendly_err = self._translate_api_error(e)
                self.root.after(0, self._append_to_gui,
                                f"Silo 2 Status: {friendly_err} (Attempt {attempt}/{MAX_ATTEMPTS}, retrying in {wait:.0f}s)")

                # Sleep in 1s slices so the Stop button stays responsive.
                slept = 0.0
                while slept < wait and self.is_running:
                    time.sleep(min(1.0, wait - slept))
                    slept += 1.0

        # is_running went False (user pressed Stop) before any success.
        return None

    def get_period_folder(self, b_id):
        b_id_str = str(b_id).strip()
        if not b_id_str or b_id_str == "0" or len(b_id_str) < 1: 
            return "Unknown-Student"
        first_char = b_id_str[0]
        if first_char in ['1', '2', '3', '4', '5', '6', '7']:
            return f"{first_char}st Period" if first_char == '1' else (f"{first_char}nd Period" if first_char == '2' else (f"{first_char}rd Period" if first_char == '3' else f"{first_char}th Period"))
        return "Unknown-Student"

    def process_assignment_names(self, title):
        internal_group_key = re.sub(r'[^a-zA-Z0-9]', '', title).lower()
        display_name = re.sub(r'[^a-zA-Z0-9]', '-', title)
        display_name = re.sub(r'-+', '-', display_name).strip('-')
        return internal_group_key, display_name if display_name else "Unnamed-Assignment"

    def generate_latex_conspectus(self, b_id, student_data, assignment_title, score, max_score, evaluation_data, original_images, output_dir, is_final_attempt=True):
        
        # --- LaTeX variable sanitization ---
        safe_tex_title = str(assignment_title).replace('&', '\\&').replace('%', '\\%').replace('#', '\\#').replace('_', '\\_')
        
        student_name = f"{student_data['first']} {student_data['last']}"
        safe_tex_name = student_name.replace('&', '\\&').replace('%', '\\%').replace('#', '\\#').replace('_', '\\_')
        
        last_name, first_name = student_data["last"], student_data["first"]
        if self.runtime is not None:
            last_name = _windows_report_name(last_name)
            first_name = _windows_report_name(first_name)
        tex_filename = f"{b_id}-{last_name}-{first_name}-Conspectus.tex"
        tex_path = os.path.join(output_dir, tex_filename)
        
        tex_content = f"\\documentclass[12pt]{{article}}\n"
        tex_content += f"\\usepackage{{fontspec}}\n"
        main_font = "Times New Roman" if self.runtime is not None else "Liberation Serif"
        tex_content += f"\\setmainfont{{{main_font}}}\n"
        tex_content += f"\\usepackage[margin=1in]{{geometry}}\n"
        tex_content += f"\\usepackage{{graphicx}}\n"
        tex_content += f"\\usepackage{{eso-pic}}\n"
        tex_content += f"\\usepackage[version=4]{{mhchem}}\n"
        tex_content += f"\\usepackage{{amsmath}}\n"
        tex_content += f"\\usepackage{{amssymb}}\n" # SURGICAL INSERTION: Advanced Math Symbols 
        tex_content += f"\\usepackage{{xcolor}}\n"
        tex_content += f"\\usepackage{{environ}}\n"
        tex_content += f"\\usepackage{{needspace}}\n\n"

        # [PATCH:overflow-wrapper] (WO-A4) An unbreakable minipage taller than
        # the page body (~650pt letter) runs off the paper: xelatex exits 0,
        # the feedback tail is lost (repro: tests/fixtures/overflow_repro/).
        # conspectusitem measures the item once; if it fits one page it emits
        # the SAME minipage box that was measured (identical metrics, identical
        # page breaks), and only a too-tall item is re-emitted as plain
        # breakable text. INVARIANT: item bodies must stay side-effect-free
        # (no \label, \footnote or counter steps) -- a too-tall \BODY is
        # typeset twice, once to measure and once to emit.
        # [PATCH:overflow-stickiness] The breakable rendering shares \BODY with
        # the fitting path, so its label/status lines cannot carry inline
        # stickiness macros. Equivalents, applied ONLY in the breakable branch:
        #   * label line = first line of the \BODY paragraph -> \clubpenalty
        #     10000 (the \nopagebreak-after-label guarantee)
        #   * STATUS line = the body's one \textcolor use -> a local \textcolor
        #     wrapper drops \vadjust{\penalty 10000} after that line (the \\*
        #     guarantee: STATUS is never last on a page)
        #   * feedback tail -> \widowpenalty 10000
        # The fitting path emits the untouched measured box: no stickiness.
        tex_content += f"% [PATCH:overflow-wrapper] keep-together when it fits, breakable only when it cannot\n"
        tex_content += f"\\newsavebox{{\\conspectusbox}}\n"
        tex_content += f"\\NewEnviron{{conspectusitem}}{{%\n"
        tex_content += f"\\sbox{{\\conspectusbox}}{{\\begin{{minipage}}{{\\textwidth}}\\BODY\\end{{minipage}}}}%\n"
        tex_content += f"\\ifdim\\dimexpr\\ht\\conspectusbox+\\dp\\conspectusbox\\relax>\\dimexpr\\textheight+1pt\\relax\n"
        tex_content += "{\\clubpenalty=10000 \\widowpenalty=10000\n"
        tex_content += "\\let\\LedgerRealTextcolor\\textcolor\n"
        tex_content += "\\def\\textcolor##1##2{\\vadjust{\\penalty 10000}\\LedgerRealTextcolor{##1}{##2}}%\n"
        tex_content += f"\\noindent\\ignorespaces\\BODY\\par}}%\n"
        tex_content += f"\\else\n"
        tex_content += f"\\noindent\\usebox{{\\conspectusbox}}%\n"
        tex_content += f"\\fi}}\n\n"

        tex_content += f"% Watermark setup\n"
        tex_content += f"\\newcommand\\BackgroundPic{{%\n"
        tex_content += f"\\put(0,0){{%\n"
        tex_content += f"\\parbox[b][\\paperheight]{{\\paperwidth}}{{%\n"
        tex_content += f"\\vfill\n"
        tex_content += f"\\centering\n"
        if os.path.exists(WATERMARK_FILE):  # [PATCH:wm-guard] missing watermark must not error every page
            watermark_path = WATERMARK_FILE.replace("\\", "/") if self.runtime is not None else WATERMARK_FILE
            tex_content += f"\\includegraphics[width=0.5\\paperwidth,keepaspectratio]{{{watermark_path}}}%\n"
        tex_content += f"\\vfill\n"
        tex_content += f"}}}}}}\n\n"

        tex_content += f"\\begin{{document}}\n"
        tex_content += f"\\AddToShipoutPictureFG{{\\BackgroundPic}}\n\n"

        tex_content += f"\\noindent\\textbf{{Name:}} {safe_tex_name}\\\\\n"
        tex_content += f"\\textbf{{Assignment:}} {safe_tex_title}\\\\\n"
        
        # Fetch and safely sanitize the custom pseudonym for LaTeX
        raw_label = self.get_custom_id_label()
        safe_tex_label = raw_label.replace('&', '\\&').replace('%', '\\%').replace('#', '\\#').replace('_', '\\_')
        tex_content += f"\\textbf{{{safe_tex_label}: }} {b_id}\\\\\n"
        tex_content += f"\\textbf{{Final Score: }} {format_score(score)}/{format_score(max_score)}\n"
        tex_content += f"\\vspace{{1cm}}\n\n"

        # [PATCH:ss-conspectus] Unselected optional prompts collapse into ONE
        # neutral gray line -- never a wall of red INCORRECTs for a student who
        # followed the choose-your-own directions. Normalizer tolerates the
        # model emitting "NOT SELECTED" / "not-selected" variants.
        def _is_not_selected(it):
            s = str(it.get('status', '')).strip().upper().replace(' ', '_').replace('-', '_')
            return s == "NOT_SELECTED"

        not_selected_items = [item for item in evaluation_data if _is_not_selected(item)]
        graded_items = [item for item in evaluation_data if not _is_not_selected(item)]

        if not_selected_items:
            ns_nums = ", ".join(str(i.get('item_number', '?')).strip().rstrip(')') for i in not_selected_items)
            ns_plural = "prompts" if len(not_selected_items) > 1 else "prompt"
            tex_content += f"\\noindent\\textbf{{\\textcolor{{gray}}{{Optional {ns_plural} not selected: {ns_nums}}}}}\n"
            tex_content += f"\\vspace{{0.5cm}}\n\n"

        # [PATCH:essay-item-pages] In ESSAY mode every graded item opens on a
        # FRESH page: item 1 shares page 1 with the score header, each later
        # item is preceded by \clearpage. An item longer than one page still
        # flows onto the next (quotation blocks remain page-breakable -- the
        # ss-paragraphs guarantee), but no page ever mixes two items. BOXED
        # mode is untouched: a 20-item chemistry worksheet must not become a
        # 20-page conspectus.
        _one_item_per_page = getattr(self, "grading_mode", "boxed") == "essay"
        _first_graded_item = True

        for item in graded_items:
            if _one_item_per_page and not _first_graded_item:
                tex_content += "\\clearpage\n\n"
            _first_graded_item = False
            status = str(item.get('status', '')).strip()
            points = item.get('points_earned', 0) # SURGICAL INSERTION: Grab the points
            
            # SURGICAL INSERTION: 3-way color logic
            if status == "CORRECT":
                color = "green!60!black"
            elif status == "PARTIAL":
                color = "orange" # Changed to perfectly align with the visual language
            else:
                color = "red"
            
            student_answer = str(item.get('student_answer', '')).strip().replace('\r\n', '\n')
            feedback = str(item.get('feedback', '')).strip()
            item_number = str(item.get('item_number', '')).strip()

            # [PATCH:ss-paragraphs] Essay-length transcriptions keep the student's
            # paragraph structure (blank-line separated in the JSON string) and
            # render OUTSIDE a minipage so LaTeX can break them across pages --
            # a giant minipage cannot break and silently overflows the page.
            # Short items keep the original minipage block verbatim.
            paragraphs = [p.strip() for p in re.split(r'\n\s*\n', student_answer) if p.strip()]
            # [PATCH:essay-render-gate] The breakable quotation layout is
            # an ESSAY-mode presentation. In WORKSHEET (BOXED) mode every
            # item -- regardless of length -- keeps the original
            # unbreakable minipage block, restoring the pre-Rev-yy
            # guarantee that a boxed response and its feedback never
            # split across a page boundary. The multi-paragraph / >600-
            # char heuristic alone was routing long MATH transcriptions
            # (LaTeX is verbose) into the essay path. In essay mode the
            # boolean is unchanged (True and x == x).
            is_essay = (getattr(self, "grading_mode", "boxed") == "essay") and \
                       (len(paragraphs) > 1 or len(student_answer) > 600)

            if is_essay:
                answer_body = "\n\\par\\smallskip\n".join(paragraphs)
                tex_content += f"\\noindent\\textbf{{{item_number}}} Student Answer:\n"
                # [PATCH:overflow-stickiness] (WO-A4) \nopagebreak: the label
                # line is never last on a page. \needspace + \\*: the STATUS
                # line always has the first feedback line with it, so neither
                # STATUS nor a lone "Feedback:" is ever stranded at a page
                # bottom. Visible diff vs pre-A4: an essay that used to break
                # at one of these seams now breaks a line earlier.
                tex_content += f"\\nopagebreak\n"
                # [PATCH:essay-first-indent] The quotation environment indents
                # EVERY paragraph by default; the old explicit \noindent here
                # suppressed exactly the first one, so a student who indented
                # all her paragraphs saw her first reprinted flush-left. Let
                # quotation do its job -- all paragraphs now indent uniformly.
                tex_content += f"\\begin{{quotation}}{answer_body}\\end{{quotation}}\n"
                tex_content += f"\\needspace{{2\\baselineskip}}\n"
                tex_content += f"\\noindent\\textbf{{\\textcolor{{{color}}}{{{status} [{format_score(points)} pts]}}}} \\\\*\n"
                tex_content += f"\\textit{{Feedback:}} {feedback}\n"
                tex_content += f"\\vspace{{0.5cm}}\n\n"
            else:
                answer_newline = "" if student_answer.endswith("\\]") else " \\\\"
                # [PATCH:overflow-wrapper] Only the two wrapper lines changed
                # vs the pre-A4 minipage block; the body lines (including the
                # \] answer_newline quirk) are byte-identical and shared by
                # both renderings through \BODY.
                tex_content += f"\\begin{{conspectusitem}}\n"
                tex_content += f"\\textbf{{{item_number}}} Student Answer: {student_answer}{answer_newline}\n"
                tex_content += f"\\textbf{{\\textcolor{{{color}}}{{{status} [{format_score(points)} pts]}}}} \\\\\n" # SURGICAL INSERTION: Print points
                tex_content += f"\\textit{{Feedback:}} {feedback}\n"
                tex_content += f"\\vspace{{0.5cm}}\n"
                tex_content += f"\\end{{conspectusitem}}\n\n"
            
        tex_content += "\\clearpage\n\n"
        
        for img in original_images:
            if self.runtime is not None:
                img = img.replace("\\", "/")
            tex_content += f"\\AddToShipoutPictureBG*{{%\n"
            tex_content += f"  \\AtPageLowerLeft{{%\n"
            tex_content += f"    \\includegraphics[width=\\paperwidth,height=\\paperheight]{{{img}}}%\n"
            tex_content += f"  }}%\n"
            tex_content += f"}}\n"
            tex_content += f"\\mbox{{}}\\clearpage\n"
            
        tex_content += "\\end{document}"
        
        success = False
        try:
            with open(tex_path, 'w', encoding='utf-8') as f:
                f.write(tex_content)
        except Exception as e:
            self.log_message(f"[!] Critical File Write Error for {b_id}: {e}")
            return False
            
        self.log_message(f"--> Compiling Conspectus PDF for {student_name} (ID: {b_id})...")
        try:
            command = [self._xelatex, '-interaction=nonstopmode']
            if self.runtime is not None:
                command.append('-no-shell-escape')
            result = subprocess.run(
                [*command, tex_filename],
                cwd=output_dir,
                capture_output=True,
                text=True,
                encoding="utf-8" if self.runtime is not None else None,
                errors="replace" if self.runtime is not None else None,
                timeout=900,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if self.runtime is not None else 0,
            )

            # [PATCH:overflow-detector] Warn-only overflow scan of the compile
            # log. Runs on success AND failure (the defect compiles "fine").
            # Never blocks, alters or fails the PDF; any error here is logged
            # and swallowed.
            try:
                log_path = os.path.join(output_dir, tex_filename.replace('.tex', '.log'))
                if os.path.exists(log_path):
                    with open(log_path, 'r', encoding='utf-8', errors='replace') as _lf:
                        _overflows = detect_vbox_overflows(_lf.read())
                    if _overflows:
                        self.log_message(
                            f"[!] OVERFLOW: conspectus for {student_name} (ID: {b_id}, "
                            f"'{assignment_title}') has {len(_overflows)} page overflow(s) up to "
                            f"{max(_overflows):.1f}pt -- content may be cut off the bottom of the page."
                        )
            except Exception as _oe:
                try:
                    self.log_message(f"[!] Overflow detector error (ignored): {_oe}")
                except Exception:
                    pass

            expected_pdf = os.path.join(output_dir, tex_filename.replace('.tex', '.pdf'))
            # [PATCH:pdf-validity] a file existing is not enough -- xelatex can leave a damaged PDF
            pdf_ok = False
            if os.path.exists(expected_pdf) and os.path.getsize(expected_pdf) > 1000:
                try:
                    _sz = os.path.getsize(expected_pdf)
                    with open(expected_pdf, 'rb') as _pf:
                        _head = _pf.read(8)
                        _pf.seek(max(0, _sz - 2048))
                        _tail = _pf.read()
                    pdf_ok = _head.startswith(b'%PDF-') and (b'%%EOF' in _tail)
                except Exception:
                    pdf_ok = False
            if pdf_ok:
                self.log_message(f"--> Success! Conspectus saved for ID: {b_id}.")
                success = True
            else:
                if os.path.exists(expected_pdf):
                    self.log_message(f"[!] Conspectus PDF for {b_id} compiled but is invalid/corrupt. Treating as failure.")
                else:
                    self.log_message(f"[!] Compilation Failed for {b_id}.")
                if is_final_attempt:
                    self.log_message("Generating Plain Text Fallback...")
                    fallback_path = os.path.join(output_dir, tex_filename.replace('.tex', '-FALLBACK.txt'))
                    with open(fallback_path, 'w', encoding='utf-8') as fb:
                        fb.write(f"Name: {student_name}\nID: {b_id}\nScore: {format_score(score)}/{format_score(max_score)}\n\n")
                        for item in evaluation_data:
                            fb.write(f"Question {item.get('item_number', '')}\n")
                            fb.write(f"Answer: {item.get('student_answer', '')}\n")
                            fb.write(f"Status: {item.get('status', '')}\n")
                            fb.write(f"Feedback: {item.get('feedback', '')}\n")
                            fb.write("-" * 40 + "\n")
                    self.log_message(f"--> Fallback text report saved for ID: {b_id}.")
                success = False
                
        except subprocess.TimeoutExpired:
            self.log_message(f"[!] LaTeX Compilation Exceeded 15 Minutes for {b_id}. Process killed to protect batch.")
            success = False
        except Exception as e:
            self.log_message(f"[!] Compilation Subprocess Error for {b_id}: {e}")
            success = False
                    
        # [PATCH:diag-retention] keep the LaTeX source + compiler log when a conspectus fails
        if success:
            for ext in ['.aux', '.log', '.tex']:
                clean_path = os.path.join(output_dir, tex_filename.replace('.tex', ext))
                if os.path.exists(clean_path): os.remove(clean_path)
        else:
            diag_dir = os.path.join(output_dir, '.Conspectus-Diagnostics')
            try:
                os.makedirs(diag_dir, exist_ok=True)
                for ext in ['.aux', '.log', '.tex', '.pdf']:
                    src = os.path.join(output_dir, tex_filename.replace('.tex', ext))
                    if os.path.exists(src):
                        shutil.move(src, os.path.join(diag_dir, os.path.basename(src)))
                self.log_message(f"    -> Diagnostics for {b_id} saved to .Conspectus-Diagnostics/")
            except Exception as _e:
                self.log_message(f"[!] Could not preserve conspectus diagnostics for {b_id}: {_e}")
            for ext in ['.aux', '.log', '.tex']:
                leftover = os.path.join(output_dir, tex_filename.replace('.tex', ext))
                if os.path.exists(leftover): os.remove(leftover)
        
        return success

    def process_pipeline(self):
        # 1. Establish Cache
        success = self.initialize_silo_2_cache(self.master_pdf_path)
        if not success:
            return

        self.log_message("Scanning '02 Inbox Scans' folder...")
        pdf_files = [f for f in os.listdir(UNEVALUATED_DIR) if f.lower().endswith('.pdf')]
        
        if not pdf_files:
            self.log_message("No unevaluated PDFs found.")
            return
            
        global_batch_data = {}
        pdf_to_period_map = {}
        tabula_records_by_eval_dir = {}
        use_desktop = self.get_silo2_model().lower().startswith(DESKTOP_MODEL_PREFIXES)
        
        self.log_message(f"=== INITIATING LEDGER PASS 1: EXTRACTION & CROPPING ({len(pdf_files)} Files) ===")
        
        for pdf_file in pdf_files:
            if not self.is_running: break
            pdf_path = os.path.join(UNEVALUATED_DIR, pdf_file)
            self.log_message(f"Reading document: {pdf_file}")
            
            first_period_found = None
                        
            try:
                doc = fitz.open(pdf_path)
                total_pages = len(doc)
                
                for page_num in range(total_pages):
                    if not self.is_running: break
                    
                    page = doc.load_page(page_num)
                    pix = page.get_pixmap(matrix=fitz.Matrix(4.16, 4.16), alpha=False) 
                    # NOTE: Images saved to TEMP_DIR, parked safely away from Ledger Engine. 
                    
                    # THE FIX: Replace spaces in the PDF filename with underscores for LaTeX
                    safe_pdf_name = pdf_file.replace(" ", "_")
                    temp_full_img_path = os.path.join(TEMP_DIR, f"{safe_pdf_name}-temp-full-page-{page_num + 1}.png")
                    
                    pix.save(temp_full_img_path)
                    
                    if ImageStat.Stat(Image.open(temp_full_img_path).convert("L")).stddev[0] < 15.0:
                        self.log_message(f"  -> Page {page_num + 1}/{total_pages} is blank. Skipping.")
                        continue
                        
                    # THE FIX: Silo 1 now runs on every single page to catch out-of-order scans!
                    # 1. Dynamic Extraction of ID and Footer using locked Database Coordinates
                    img_for_crop = _read_cv_image(temp_full_img_path)
                    
                    id_coords = self.get_crop_coordinates('crop_id')
                    id_crop = img_for_crop[id_coords[1] : id_coords[1]+id_coords[3], id_coords[0] : id_coords[0]+id_coords[2]]
                    temp_id_path = os.path.join(TEMP_DIR, f"{safe_pdf_name}-temp-id-{page_num + 1}.png")
                    _write_cv_image(temp_id_path, id_crop)

                    f_coords = self.get_crop_coordinates('crop_footer')
                    footer_crop = img_for_crop[f_coords[1] : f_coords[1]+f_coords[3], f_coords[0] : f_coords[0]+f_coords[2]]
                    temp_footer_path = os.path.join(TEMP_DIR, f"{safe_pdf_name}-temp-footer-{page_num + 1}.png")
                    _write_cv_image(temp_footer_path, footer_crop)

                    # 2. Extract data (local OCR for the footer, provider OCR for the ID)
                    if not use_desktop:
                        time.sleep(30) # Prevent 429 quota exhaustion
                    silo_data = self.extract_with_silo_1(temp_id_path, temp_footer_path)
                    if use_desktop and not self.is_running:
                        doc.close()
                        return
                    
                    if silo_data:
                        b_id = str(silo_data.get("bartonian_id", 0))
                        if b_id not in self.roster:
                            self.log_message(f"  -> [!] Invalid/missing ID ({b_id}). Discarding page.")
                            continue
                            
                        if not first_period_found:
                            first_period_found = self.get_period_folder(b_id)
                            
                        # Use the globally locked Master Title variables here
                        if self.master_group_key not in global_batch_data:
                            global_batch_data[self.master_group_key] = {
                                "display_title": self.master_display_title,
                                "original_title": self.master_original_title,
                                "max_possible_score": self.global_max_score,
                                "students": {}
                            }
                            
                        assignment_ref = global_batch_data[self.master_group_key]
                        
                        if b_id not in assignment_ref["students"]:
                            assignment_ref["students"][b_id] = {
                                "full_pages": [],  
                                "snippets": []     
                            }
                            
                        # 3. NEW: Get the page number from the Silo 1 JSON
                        silo_page_number = silo_data.get("page_num", 999)
                        
                        # [PATCH:essay-mode] FULL_PAGE mode never crops boxes:
                        # the page itself is the payload. Instead, locally OCR
                        # the printed corner tag so pages can be grouped into
                        # essays (and continuations detected) with zero API
                        # calls. BOXED mode is byte-path identical to before.
                        if getattr(self, "grading_mode", "boxed") == "essay":
                            # [PATCH:essay-tag-quiet] OCR the corner tag ONLY
                            # when chunked packing will consult it; in
                            # single-call mode skip the work and the noise.
                            if getattr(self, "essay_tag_ocr_needed", False):
                                essay_tag = self._ocr_essay_tag(temp_full_img_path)
                                tag_note = (f", Essay Tag: "
                                            f"{essay_tag if essay_tag is not None else 'not detected'}")
                            else:
                                essay_tag = None
                                tag_note = ""
                            assignment_ref["students"][b_id]["full_pages"].append({
                                "path": temp_full_img_path,
                                "lowest_q": silo_page_number,
                                "essay_num": essay_tag
                            })
                            self.log_message(f"  -> Page sorted to ID {b_id} "
                                             f"(Page Number Read: {silo_page_number}{tag_note})")
                        else:
                            assignment_ref["students"][b_id]["full_pages"].append({
                                "path": temp_full_img_path, 
                                "lowest_q": silo_page_number # Reusing existing variable key for seamless sorting
                            })
                            
                            page_snippets = self.extract_boxes_with_opencv(temp_full_img_path, page_num + 1)
                            assignment_ref["students"][b_id]["snippets"].extend(page_snippets)
                            # [PATCH:silo2-per-chunk-context] Record the FOOTER-derived
                            # master page number for every snippet cropped from this
                            # physical page. The scan-stack index in the snippet
                            # filename is NOT the master page; only the printed
                            # footer number is. 999 (unreadable) is recorded as-is
                            # and triggers the FULL-master fallback downstream.
                            _smap = assignment_ref["students"][b_id].setdefault("snippet_master_pages", {})
                            for _sp in page_snippets:
                                _smap[_sp] = silo_page_number
                            
                            self.log_message(f"  -> Page sorted to ID {b_id} (Page Number Read: {silo_page_number})")
                                                                            
                doc.close()
                pdf_to_period_map[pdf_file] = first_period_found if first_period_found else "Unknown-Student"
                
            except DesktopOcrError as e:
                doc.close()
                self.log_message(f"[!] Desktop OCR stopped the batch. Scans are still in the inbox. {e}")
                return
            except Exception as e:
                self.log_message(f"[!] Error reading {pdf_file}: {e}")
                continue
                
        # PASS 2: Evaluation & Conspectus Generation
        self.log_message("=== PASS 1 COMPLETE. INITIATING LEDGER PASS 2: BATCH EVALUATION ===")
        
        deferred_ids = set()  # [PATCH:silo2-bounded-retry] students skipped on capacity (429)
        for group_key, data in global_batch_data.items():
            max_possible_score = data["max_possible_score"]
            safe_title = data["display_title"]
            
            for b_id, student_data in data["students"].items():
                if not self.is_running: break
                
                period_folder = self.get_period_folder(b_id)
                
                # --- FOLDER NOMENCLATURE SURGERY ---
                if period_folder == "Unknown-Student":
                    folder_suffix = "-Unknown"
                else:
                    # Splits "7th Period" at the space and grabs just "7th"
                    folder_suffix = f"-{period_folder.split()[0]}" 
                    
                # Append the suffix for the folder name, leaving safe_title intact for LaTeX
                appended_folder_name = f"{safe_title}{folder_suffix}"
                
                eval_dir = os.path.join(COURSES_DIR, period_folder, "Evaluated Assignments", appended_folder_name)
                # -----------------------------------
                
                os.makedirs(eval_dir, exist_ok=True)
                
                # [PATCH:essay-mode] Assemble the Silo 2 payload for this
                # student. BOXED: stitched grid chunks (single image per call,
                # unchanged). ESSAY: essay-aligned groups of resized full
                # pages (one page LIST per call; evaluate_grid_with_silo_2
                # ships a list as an images_b64 payload).
                if getattr(self, "grading_mode", "boxed") == "essay":
                    chunk_paths = self.assemble_essay_pages(student_data["full_pages"], b_id)
                else:
                    chunk_paths = self.stitch_snippets_to_grid(student_data["snippets"], b_id,
                                                               student_data.get("snippet_master_pages"))
                
                if not chunk_paths:
                    self.log_message(f"[!] No snippets found for {b_id}. Skipping evaluation.")
                    continue

                # --- NEW RETRY LOOP INITIATED ---
                max_attempts = 3
                attempt = 1
                compilation_success = False
                
                # Pre-sort original pages and pull student info outside the evaluation loop
                student_data["full_pages"].sort(key=lambda x: x["lowest_q"])
                sorted_page_paths = [page["path"] for page in student_data["full_pages"]]
                student_info = self.roster.get(b_id, {"last": "Unknown", "first": "Student"})

                full_evaluation = None
                while attempt <= max_attempts and self.is_running:
                    self.log_message(f"Evaluating stitched grid for ID {b_id} via cached Silo 2 (Attempt {attempt} of {max_attempts})...")
                    
                    if not use_desktop:
                        time.sleep(15 if attempt > 1 else 30)
                    if full_evaluation is None or not use_desktop:
                        full_evaluation = self.evaluate_chunks_with_silo_2(chunk_paths)
                    if not self.is_running:
                        break
                    
                    # [PATCH:silo2-bounded-retry] None == bounded retries exhausted on Google
                    # capacity (429), OR the user pressed Stop. Defer this student: do NOT write
                    # a zero grade or build a conspectus from empty results.
                    if full_evaluation is None:
                        if self.is_running:
                            self.log_message(f"  -> [DEFERRED] ID {b_id}: grading call failed (see Silo 2 Detail above) - not graded. Scan retained for re-run.")
                            deferred_ids.add(b_id)
                        break  # leave the LaTeX-attempt loop for this student

                    # NEW GUARD: Prevent fall-through crash on NoneType objects if API request fails
                    if not isinstance(full_evaluation, list):
                        full_evaluation = []
                    
                    # Post-evaluation sorting (Alphanumeric)
                    if full_evaluation:
                        prefix_map = {}
                        for item in full_evaluation:
                            m = re.match(r'^(\d+)([a-z]+)', str(item.get("item_number", "")).strip().lower())
                            if m:
                                prefix = int(m.group(1))
                                prefix_map[prefix] = prefix_map.get(prefix, 0) + 1
                        best_prefix = max(prefix_map.keys(), key=lambda k: prefix_map[k]) if prefix_map else 0
                        
                        def get_item_sort_key(item):
                            s = str(item.get("item_number", "")).strip().lower()
                            m_num = re.match(r'^(\d+)([a-z]*)', s)
                            if m_num: return (int(m_num.group(1)), m_num.group(2))
                            m_let = re.match(r'^([a-z]+)', s)
                            if m_let: return (best_prefix if best_prefix > 0 else float('inf'), m_let.group(1))
                            return (float('inf'), s)
                        full_evaluation.sort(key=get_item_sort_key)
                    
                    earned_points = sum(item.get("points_earned", 0) for item in full_evaluation)
                    self.log_message(f"  -> Score Computed: {format_score(earned_points)}/{format_score(max_possible_score)}")
                    
                    # NEW: Write the final score to the database
                    self.write_grade_to_db(b_id, group_key, earned_points)
                    
                    # Conspectus generation with success tracking
                    is_final = (attempt == max_attempts)
                    compilation_success = self.generate_latex_conspectus(
                        b_id, student_info, data["original_title"], earned_points, 
                        max_possible_score, full_evaluation, sorted_page_paths, eval_dir,
                        is_final_attempt=is_final
                    )
                    
                    if compilation_success:
                        break # Success! Exit the retry loop.
                    else:
                        self.log_message(f"[!] LaTeX Compilation Failed. Re-triggering Silo 2...")
                        attempt += 1

                if not self.is_running: break # Safety exit for shutdown button

                # [PATCH:silo2-bounded-retry] Deferred students were never graded. Record
                # them in the Tabula as DEFERRED so the teacher sees exactly who needs a
                # re-run, then skip the normal score append below.
                if b_id in deferred_ids:
                    if eval_dir not in tabula_records_by_eval_dir:
                        tabula_records_by_eval_dir[eval_dir] = []
                    tabula_records_by_eval_dir[eval_dir].append({
                        "last": student_info['last'], "first": student_info['first'],
                        "b_id": b_id, "score_str": "DEFERRED"
                    })
                    continue
                
                # Tabula Generation proceeds with the final evaluation result
                if eval_dir not in tabula_records_by_eval_dir:
                    tabula_records_by_eval_dir[eval_dir] = []
                tabula_records_by_eval_dir[eval_dir].append({
                    "last": student_info['last'], "first": student_info['first'], 
                    "b_id": b_id, "score_str": f"{format_score(earned_points)}/{format_score(max_possible_score)}"
                })
                
        # Tabula Generation
        if self.is_running:
            for eval_dir, records in tabula_records_by_eval_dir.items():
                tabula_path = os.path.join(eval_dir, "Tabula.csv")
                merged_data = {}
                
                # 1. Load existing data to prevent overwriting previous batches
                if os.path.exists(tabula_path):
                    try:
                        with open(tabula_path, 'r', encoding='utf-8') as f:
                            reader = csv.reader(f)
                            next(reader, None) # Skip the header row
                            for row in reader:
                                if len(row) >= 4:
                                    # SURGICAL INSERTION: Aggressively strip anything that isn't a digit
                                    b_id = ''.join(filter(str.isdigit, row[2]))
                                    merged_data[b_id] = {
                                        "last": row[0], "first": row[1], 
                                        "b_id": b_id, "score_str": row[3].replace("'", "")
                                    }
                    except Exception as e:
                        self.log_message(f"[!] Warning: Could not read existing Tabula: {e}")

                # 2. Merge new records (safely overwriting duplicates via ID key)
                for rec in records:
                    merged_data[rec["b_id"]] = rec
                    
                # [PATCH:final-records] materialize the dedupe dict the sort/write below expect
                final_records = list(merged_data.values())
                
                # 3. Sort the combined master list alphabetically (Falls back to ID for blank names/ties)
                final_records.sort(key=lambda x: (x["last"].lower(), x["first"].lower(), x["b_id"]))
                final_records.sort(key=lambda x: (x["last"].lower(), x["first"].lower()))
                
                # 4. Write the combined data back to the CSV
                try:
                    with open(tabula_path, 'w', newline='', encoding='utf-8') as f:
                        writer = csv.writer(f)
                        writer.writerow(["Last Name", "First Name", "Bartonian ID", "Final Score"])
                        for rec in final_records:
                            # Prepending an apostrophe to force left-alignment (text format) in Calc without visible quotes
                            writer.writerow([rec["last"], rec["first"], f"'{rec['b_id']}", f"'{rec['score_str']}"])
                except PermissionError:
                    self.log_message(f"[!] CRITICAL: Could not save Tabula.csv. Is it currently open in another program?")
                except Exception as e:
                    self.log_message(f"[!] Error writing Tabula: {e}")

        # Preserve scans when the operator stops before completing the batch.
        if not self.is_running:
            return

        # Original Scan Cleanup
        self.log_message("=== PASS 2 COMPLETE. MOVING ORIGINAL SCANS ===")
        if deferred_ids:
            self.log_message(f"=== {len(deferred_ids)} STUDENT(S) DEFERRED (grading call failed). Scans RETAINED in '02 Inbox Scans' for re-run; grades use INSERT OR REPLACE and the Tabula de-dupes by ID, so re-running the batch is idempotent: {sorted(deferred_ids)} ===")
        for pdf_file, dest_period in pdf_to_period_map.items():
            if deferred_ids:
                continue  # [PATCH:silo2-bounded-retry] retain scans while any student is deferred
            source_path = os.path.join(UNEVALUATED_DIR, pdf_file)
            if os.path.exists(source_path):
                try:
                    dest_path = os.path.join(ORIGINAL_SCANS_DIR, pdf_file)
                    shutil.move(source_path, dest_path)
                except Exception as e:
                    self.log_message(f"[!] Could not move scan {pdf_file}: {e}")


    def finish_processing(self):
        try:
            self.delete_cache()
            self.clean_temp_directory()
        except OSError:
            self.log_message("[!] Batch cleanup could not clear the temporary folder.")
        finally:
            self.is_running = False
            self._batch_pending = False
            self._batch_model = None
            self.update_indicator("idle")
            self.run_btn.configure(state="normal")
            self.max_score_entry.configure(state="normal")
            self.mode_selector.configure(state="normal")
            self.master_btn.configure(state="normal")
            self.model_btn.configure(state="normal")
        self.log_message("=== BATCH PROCESSING CONCLUDED. ===")

def _run_desktop():
    root = ctk.CTk()
    root.withdraw()
    try:
        with DesktopRuntime() as runtime:
            startup_error = None
            if runtime.init_path.is_file():
                try:
                    runtime.start()
                except (OSError, RuntimeError) as exc:
                    startup_error = str(exc)
            LedgerGUI(root, runtime=runtime)
            root.deiconify()
            if startup_error is not None:
                messagebox.showerror("Ledger daemon", startup_error, parent=root)
            root.mainloop()
    except (OSError, RuntimeError) as exc:
        messagebox.showerror("Ledger", str(exc), parent=root)
        root.destroy()


if __name__ == "__main__" and IS_LOCAL_DESKTOP:
    _run_desktop()
elif __name__ == "__main__":
    import socket
    
    # --- PARAGON SINGLE INSTANCE LOCK ---
    try:
        # Create a Linux Abstract Socket (denoted by the leading \0)
        # It exists only in RAM and is instantly destroyed if the app or OS crashes.
        single_instance_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        single_instance_socket.bind('\0paragon_ledger_lock')
    except socket.error:
        # The socket is already claimed. Bring the existing window to the front.
        try:
            # -a searches for the exact window title, switches workspaces if needed, and raises it
            subprocess.run(['wmctrl', '-a', 'The Ledger'], check=False)
        except FileNotFoundError:
            pass # Fails gracefully if wmctrl happens to be missing
        
        sys.exit(0)
        
    root = ctk.CTk()
    app = LedgerGUI(root)
    root.mainloop()
