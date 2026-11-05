#!/bin/bash
# build-ledger.sh -- The Ledger, INDIVIDUAL ("teacher-friendly") build, Layer 1, Debian 13.
#
# Repeats, unattended, the steps that built and passed acceptance on LEDGER-DISTRO
# (2026-10-24). Every step comes from BUILD_LOG.md.
#
# DRY-RUN BY DEFAULT: it only prints what it would do. Nothing changes without --commit.
#
#   sudo bash build-ledger.sh                  # dry run (safe; prints the plan)
#   sudo bash build-ledger.sh --commit         # really build
#   sudo bash build-ledger.sh --commit --source /path/to/ledger-source
#
# Run it from the desktop user's own terminal with sudo (you type your password once).
# The desktop user must be named "paragon" (the grader's permission checks use that name).
# Safe to re-run: every step checks what is already there first.
# Not done by this script: apt upgrade, apt autoremove, any .env / API key (the grader's
# SYSTEM INITIALIZATION wizard writes those), tests/ (development only), anything appliance-only.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="$SCRIPT_DIR/ledger-source"
[[ -d "$SOURCE" ]] || SOURCE="$SCRIPT_DIR"   # public repository: engine/, daemon/, system/ sit next to this script
REQ="$SCRIPT_DIR/requirements.txt"
COMMIT=0
OWNER="paragon"
OPT=/opt/ledger
APPS=/usr/local/share/applications

# md5 of the source files this script was proven against (BUILD_LOG Phase 0 / Phase 4).
GUI_SRC_MD5="4278bf097f05f63535298ab40162a225"     # engine/ledger-main-minimus.py from clone wo-a7-gemini-devapi
GUI_DONE_MD5="447aa2ba1acd7f0fa81c4dcfbe4b22a2"    # same file after the Phase 4 change (commit ea79701)
DAEMON_MD5="ba87a2bab0235905c89b50f5f13ea7ca"      # daemon/vertex-api-daemon.py, v2.9
GUI_WO2_MD5="a203148b26bbe18b5c71a8e991a3c62b"     # GUI after WO2 + WO3 (master dc1ad54)
DAEMON_WO2_MD5="552951d9cf33b1c2ddb981d335bd2e95"  # daemon after WO2 (one comment line; WO3 left it alone, master dc1ad54)
GUI_AZURE_MD5="f32639e88ff5972b31b5335fe239a4b7"
DAEMON_AZURE_MD5="3a2ab58b411c1c232a324bc8fca5c31a"
AZURE_PROVIDER_MD5="cdf5eba1219540ed74b46ff6e269de9b"
GUI_CODEX_MD5="781daaad5b8d0f2264dfcef67c98af4f"
DAEMON_CODEX_MD5="7d6eae5bb126b67ced9e3491c412ca9a"
AZURE_PROVIDER_CODEX_MD5="8af269e1782c8a05d98d68780f21a207"
CODEX_PROVIDER_MD5="152aed9cc226985b5c977a24b00abacb"
GRADING_SCHEMA_MD5="81f37ad558a1baa973242a152a6867c1"
CODEX_SKILL_MD5="6b2e8f037d8e7994aa73af324a031621"

APT_PKGS=(python3-venv python3-tk python3-dev tesseract-ocr tesseract-ocr-eng zenity wmctrl
          texlive-xetex texlive-latex-extra texlive-science texlive-fonts-recommended
          libgl1 libglib2.0-0t64 fonts-liberation)   # last three were already on a fresh install
GROUPS_NEW=(ledger-teacher faculty-admin district-admin)
ENGINE_SKIP=(ledger-keymgr-gui.py ledger-firstlogin-passwd.py Start_KeyMgr.desktop)   # appliance-only
WORKSPACE_DIRS=("01 Master Assignments" "02 Inbox Scans" "03 Classes" "04 SIS Exports" "05 Archive Originals")

while (( $# )); do
    case "$1" in
        --commit) COMMIT=1 ;;
        --source) SOURCE="$2"; shift ;;
        -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1 (try --help)" >&2; exit 2 ;;
    esac
    shift
done

LOG="$SCRIPT_DIR/build-ledger-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1

log()  { printf '%s  %s\n' "$(date +%H:%M:%S)" "$*"; }
die()  { log "REFUSING: $*"; exit 1; }
# act "what" cmd args...  -- prints in dry-run, runs with --commit
act() {
    local what="$1"; shift
    if (( COMMIT )); then log "DO     $what"; else log "WOULD  $what"; fi
    log "       \$ $(printf '%q ' "$@")"
    if (( COMMIT )); then "$@"; fi
}
as_owner() { runuser -u "$OWNER" -- "$@"; }
skip() { log "OK     $* (already done)"; }

# ---------------------------------------------------------------- 0. preflight (read-only)
log "Ledger INDIVIDUAL build, Layer 1 -- mode: $( (( COMMIT )) && echo COMMIT || echo DRY-RUN )  log: $LOG"
[[ $EUID -eq 0 ]] || die "run with sudo: sudo bash $0   (add --commit to really build)"
. /etc/os-release
[[ "${ID:-}" == "debian" && "${VERSION_ID:-}" == "13" ]] || die "this is not Debian 13 (found: ${PRETTY_NAME:-unknown})"
[[ "${SUDO_USER:-}" == "$OWNER" ]] || die "run this from the '$OWNER' account with sudo (found: '${SUDO_USER:-root}')"
OWNER_HOME="$(getent passwd "$OWNER" | cut -d: -f6)"
for f in engine/ledger-main-minimus.py daemon/vertex-api-daemon.py system/ledger-daemon.service \
         system/ledger-start.desktop system/ledger-shutdown.desktop system/ledger-start.svg \
         system/ledger-shutdown.svg ledger-wallpaper.png; do
    [[ -f "$SOURCE/$f" ]] || die "missing $SOURCE/$f (use --source DIR)"
done
[[ -f "$REQ" ]] || die "missing $REQ"
command -v git >/dev/null || die "git is not installed: sudo apt install git"
GUI_NOW="$(md5sum < "$SOURCE/engine/ledger-main-minimus.py" | cut -d' ' -f1)"
DAEMON_NOW="$(md5sum < "$SOURCE/daemon/vertex-api-daemon.py" | cut -d' ' -f1)"
check_source_md5() {
    local file="$1" expected="$2"
    [[ -f "$SOURCE/$file" ]] || die "missing $SOURCE/$file"
    [[ "$(md5sum < "$SOURCE/$file" | cut -d' ' -f1)" == "$expected" ]] \
        || die "$file does not match the build"
}
AZURE_BUILD=0
CODEX_BUILD=0
if [[ "$GUI_NOW" == "$GUI_CODEX_MD5" && "$DAEMON_NOW" == "$DAEMON_CODEX_MD5" ]]; then
    check_source_md5 daemon/azure_provider.py "$AZURE_PROVIDER_CODEX_MD5"
    check_source_md5 daemon/codex_provider.py "$CODEX_PROVIDER_MD5"
    check_source_md5 daemon/grading_schema.py "$GRADING_SCHEMA_MD5"
    check_source_md5 .agents/skills/ledger-grading/SKILL.md "$CODEX_SKILL_MD5"
    FINAL=1
    AZURE_BUILD=1
    CODEX_BUILD=1
elif [[ "$GUI_NOW" == "$GUI_AZURE_MD5" && "$DAEMON_NOW" == "$DAEMON_AZURE_MD5" ]]; then
    check_source_md5 daemon/azure_provider.py "$AZURE_PROVIDER_MD5"
    FINAL=1
    AZURE_BUILD=1
elif [[ "$GUI_NOW" == "$GUI_WO2_MD5" && "$DAEMON_NOW" == "$DAEMON_WO2_MD5" ]]; then
    FINAL=1   # the public-repository tree: already the accepted build, nothing to patch
elif [[ "$GUI_NOW" == "$GUI_SRC_MD5" && "$DAEMON_NOW" == "$DAEMON_MD5" ]]; then
    FINAL=0   # the original clone tree: the two embedded patches bring it to the accepted build
else
    die "engine/daemon are not a version this script was tested with"
fi
log "OK     Debian $VERSION_ID, user $OWNER, source $SOURCE ($( (( FINAL )) && echo 'accepted build, no patches needed' || echo 'original tree, patches will be applied'))"

# ---------------------------------------------------------------- 1. packages
act "refresh package lists" apt-get update
missing=()
for p in "${APT_PKGS[@]}"; do
    dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q 'install ok installed' || missing+=("$p")
done
if (( ${#missing[@]} )); then
    act "install ${#missing[@]} packages (no upgrade, no autoremove)" \
        env DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}"
else
    skip "all ${#APT_PKGS[@]} packages installed"
fi
log "NOTE   fonts-ubuntu and xreader are not in Debian; not needed (GUI falls back; Atril opens PDFs)"
if ls /etc/xdg/autostart/polkit-*.desktop >/dev/null 2>&1; then
    log "OK     polkit agent present: $(ls /etc/xdg/autostart/polkit-*.desktop | xargs -n1 basename | tr '\n' ' ')"
else
    log "WARN   no polkit agent autostart found -- the setup wizard's password box (pkexec) needs one"
fi

# ---------------------------------------------------------------- 2. groups, paths
for g in "${GROUPS_NEW[@]}"; do
    if getent group "$g" >/dev/null; then skip "group $g"; else act "create group $g" groupadd "$g"; fi
done
log "NOTE   $OWNER is deliberately NOT added to ledger-teacher (owner, as on the appliance)"
act "create $OPT/{engine,daemon,venv}" mkdir -p "$OPT/engine" "$OPT/daemon" "$OPT/venv"
act "own $OPT by $OWNER:ledger-teacher" chown -R "$OWNER:ledger-teacher" "$OPT"
act "close $OPT to others" chmod -R u=rwX,g=rX,o= "$OPT"
act "create /etc/paragon (root:$OWNER 0750, no .env)" install -d -o root -g "$OWNER" -m 0750 /etc/paragon

# ---------------------------------------------------------------- 3. venv
if [[ -x "$OPT/venv/bin/python" ]]; then
    skip "venv $OPT/venv"
else
    act "create the venv" as_owner python3 -m venv "$OPT/venv"
fi
act "install pinned Python packages" as_owner "$OPT/venv/bin/python" -m pip install -q -r "$REQ"
act "check Python dependencies" as_owner "$OPT/venv/bin/python" -m pip check

# ---------------------------------------------------------------- 4. source copy + git (only on a fresh /opt/ledger)
if [[ -d "$OPT/.git" ]]; then
    skip "$OPT is already a git repo -- engine/daemon left untouched"
else
    for f in "$SOURCE"/engine/*; do
        b="$(basename "$f")"
        if printf '%s\n' "${ENGINE_SKIP[@]}" | grep -qxF "$b"; then
            log "SKIP   engine/$b (appliance-only)"
        else
            act "copy engine/$b" cp -- "$f" "$OPT/engine/"
        fi
    done
    act "copy daemon" cp -- "$SOURCE/daemon/vertex-api-daemon.py" "$OPT/daemon/"
    if (( AZURE_BUILD )); then
        act "copy Azure OpenAI provider" cp -- "$SOURCE/daemon/azure_provider.py" "$OPT/daemon/"
    fi
    if (( CODEX_BUILD )); then
        act "copy Codex handoff and grading schema" cp -- "$SOURCE/daemon/codex_provider.py" \
            "$SOURCE/daemon/grading_schema.py" "$OPT/daemon/"
        act "copy Ledger grading skill" cp -- "$SOURCE/.agents/skills/ledger-grading/SKILL.md" \
            "$OPT/daemon/ledger-grading.SKILL.md"
    fi
    act "own copied files" chown -R "$OWNER:ledger-teacher" "$OPT/engine" "$OPT/daemon"

    act "git init" as_owner git -C "$OPT" init -q -b master
    act "write .gitignore (venv is never committed)" \
        as_owner bash -c "printf 'venv/\n__pycache__/\n*.pyc\n' > '$OPT/.gitignore'"
    act "git identity (this repo only)" as_owner git -C "$OPT" config user.name "$OWNER"
    act "git identity email (this repo only)" \
        as_owner git -C "$OPT" config user.email "${LEDGER_GIT_EMAIL:-$OWNER@$(hostname)}"
    act "baseline commit" as_owner bash -c \
        "cd '$OPT' && git add -A && git commit -q -m '$( (( FINAL )) && echo 'v1.0.0: public release tree' || echo 'baseline: copied tree from clone wo-a7-gemini-devapi')'"
    if (( FINAL )); then
        log "OK     source is already the accepted build -- patch steps skipped"
    else
    act "branch for the one GUI change" as_owner git -C "$OPT" checkout -q -b debian-hide-appliance-buttons
    PATCH="$(mktemp)"
    base64 -d > "$PATCH" <<'B64'
ZGlmZiAtLWdpdCBhL2VuZ2luZS9sZWRnZXItbWFpbi1taW5pbXVzLnB5IGIvZW5naW5lL2xlZGdl
ci1tYWluLW1pbmltdXMucHkKaW5kZXggZGU1N2NmYi4uYTMxMDkzNCAxMDA3NTUKLS0tIGEvZW5n
aW5lL2xlZGdlci1tYWluLW1pbmltdXMucHkKKysrIGIvZW5naW5lL2xlZGdlci1tYWluLW1pbmlt
dXMucHkKQEAgLTY4NywxNCArNjg3LDE3IEBAIGNsYXNzIExlZGdlckdVSToKICAgICAgICAgICAg
IGFkbWluX2dyb3VwcyA9IFsnZmFjdWx0eS1hZG1pbicsICdkaXN0cmljdC1hZG1pbicsICdzdWRv
J10KICAgICAgICAgICAgIHVzZXJfZ3JvdXBzID0gW2cuZ3JfbmFtZSBmb3IgZyBpbiBncnAuZ2V0
Z3JhbGwoKSBpZiBjdXJyZW50X3VzZXIgaW4gZy5ncl9tZW1dCiAgICAgICAgICAgICBpc19hZG1p
biA9IGN1cnJlbnRfdXNlciA9PSAncGFyYWdvbicgb3IgYW55KGdyb3VwIGluIHVzZXJfZ3JvdXBz
IGZvciBncm91cCBpbiBhZG1pbl9ncm91cHMpCisgICAgICAgICAgICAjIEtleXMgJiBBY2NvdW50
cyBidXR0b25zIGV4aXN0IG9ubHkgb24gdGhlIGhhcmRlbmVkIGFwcGxpYW5jZSBidWlsZCAoa2V5
bWdyIEdVSSArIHVzZXJtZ3IgaGVscGVyKS4KKyAgICAgICAgICAgIGFwcGxpYW5jZV90b29scyA9
IChvcy5wYXRoLmV4aXN0cygiL29wdC9sZWRnZXIvZW5naW5lL2xlZGdlci1rZXltZ3ItZ3VpLnB5
IikKKyAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICBhbmQgb3MucGF0aC5leGlzdHMoIi91
c3IvbG9jYWwvc2Jpbi9sZWRnZXItdXNlcm1nciIpKQogCi0gICAgICAgICAgICBpZiBpc19hZG1p
bjoKKyAgICAgICAgICAgIGlmIGFwcGxpYW5jZV90b29scyBhbmQgaXNfYWRtaW46CiAgICAgICAg
ICAgICAgICAgc2VsZi5tYW5hZ2VfdXNlcnNfYnRuID0gY3RrLkNUa0J1dHRvbigKICAgICAgICAg
ICAgICAgICAgICAgc2VsZi5zaWRlYmFyLCB0ZXh0PSJNQU5BR0UgT1MgVVNFUlMiLCBmb250PXNl
bGYubWFpbl9mb250LCAKICAgICAgICAgICAgICAgICAgICAgZmdfY29sb3I9IiMzMzMzMzMiLCBo
b3Zlcl9jb2xvcj0iIzU1NTU1NSIsIGNvbW1hbmQ9c2VsZi5vcGVuX2tleXNfYW5kX2FjY291bnRz
CiAgICAgICAgICAgICAgICAgKQogICAgICAgICAgICAgICAgIHNlbGYubWFuYWdlX3VzZXJzX2J0
bi5ncmlkKHJvdz0xMSwgY29sdW1uPTAsIHBhZHg9MjAsIHBhZHk9KDEwLCAxMCkpCi0gICAgICAg
ICAgICBlbHNlOgorICAgICAgICAgICAgZWxpZiBhcHBsaWFuY2VfdG9vbHM6CiAgICAgICAgICAg
ICAgICAgc2VsZi5lbnJvbGxfa2V5X2J0biA9IGN0ay5DVGtCdXR0b24oCiAgICAgICAgICAgICAg
ICAgICAgIHNlbGYuc2lkZWJhciwgdGV4dD0iUkVHSVNURVIgTVkgS0VZIiwgZm9udD1zZWxmLm1h
aW5fZm9udCwgCiAgICAgICAgICAgICAgICAgICAgIGZnX2NvbG9yPSIjMzMzMzMzIiwgaG92ZXJf
Y29sb3I9IiM1NTU1NTUiLCBjb21tYW5kPXNlbGYub3Blbl9rZXlzX2FuZF9hY2NvdW50cwo=
B64
    chmod 0644 "$PATCH"
    act "apply: hide MANAGE OS USERS / REGISTER MY KEY unless appliance tools exist" \
        as_owner git -C "$OPT" apply "$PATCH"
    if (( COMMIT )); then
        [[ "$(md5sum < "$OPT/engine/ledger-main-minimus.py" | cut -d' ' -f1)" == "$GUI_DONE_MD5" ]] \
            || die "patched GUI does not match the accepted build"
        as_owner "$OPT/venv/bin/python" -c "import sys; compile(open(sys.argv[1]).read(), sys.argv[1], 'exec')" \
            "$OPT/engine/ledger-main-minimus.py" && log "OK     patched GUI matches the accepted build and compiles"
    fi
    act "commit the GUI change" as_owner bash -c \
        "cd '$OPT' && git add engine/ledger-main-minimus.py && git commit -q -m 'Hide MANAGE OS USERS / REGISTER MY KEY unless appliance tools are installed'"
    act "merge into master (fast-forward)" as_owner bash -c \
        "cd '$OPT' && git checkout -q master && git merge -q --ff-only debian-hide-appliance-buttons"
    rm -f "$PATCH"

    # WO2 + WO3 (2026-10-24) = git diff ea79701 dc1ad54 on LEDGER-DISTRO (9 commits there, one here):
    # WO2: calibration studio (close-cancels-run, no box creep, fits the screen, RUN skips the
    # studio after a save this session, SAVE label, seed positions), AQ./AIza key prefixes,
    # cache-floor comment. WO3: no built-in grading prompt (constant and engine/silo2_prompt.txt
    # removed; the teacher's GRADING INSTRUCTIONS live in the DB; RUN refuses while empty).
    act "branch for the WO2 defect pass" as_owner git -C "$OPT" checkout -q -b wo2-defects
    PATCH="$(mktemp)"
    base64 -d > "$PATCH" <<'B64'
ZGlmZiAtLWdpdCBhL2RhZW1vbi92ZXJ0ZXgtYXBpLWRhZW1vbi5weSBiL2RhZW1vbi92ZXJ0ZXgt
YXBpLWRhZW1vbi5weQppbmRleCA1OTc4MDg4Li4xNWJmYzNjIDEwMDc1NQotLS0gYS9kYWVtb24v
dmVydGV4LWFwaS1kYWVtb24ucHkKKysrIGIvZGFlbW9uL3ZlcnRleC1hcGktZGFlbW9uLnB5CkBA
IC02NjYsNyArNjY2LDcgQEAgZGVmIGNhY2hlX2J1aWxkKCk6CiAgICAgICAgICAgICAgICAgIyBb
UEFUQ0g6aW1wbGljaXQtY2FjaGUtb25seV0gQXJnb24tY2xhc3MgbW9kZWw6IG5vIGV4cGxpY2l0
CiAgICAgICAgICAgICAgICAgIyBjYWNoZSBleGlzdHMuIE1hc3RlcnMgcmlkZSBpbmxpbmUgYXQg
dGhlIEZST05UIG9mIGV2ZXJ5CiAgICAgICAgICAgICAgICAgIyBjYWxsLCB3aGVyZSBWZXJ0ZXgn
cyBhdXRvbWF0aWMgaW1wbGljaXQgY2FjaGUgcGlja3MgdGhlbQotICAgICAgICAgICAgICAgICMg
dXAgKD49IDYsMTQ0LXRva2VuIHByZWZpeCkuIFdhdGNoICdHb29nbGUgdXNhZ2U6IGNhY2hlZD0n
LgorICAgICAgICAgICAgICAgICMgdXAgKD49IDQsMDk2LXRva2VuIHByZWZpeCkuIFdhdGNoICdH
b29nbGUgdXNhZ2U6IGNhY2hlZD0nLgogICAgICAgICAgICAgICAgIGxvZy5pbmZvKAogICAgICAg
ICAgICAgICAgICAgICAiTW9kZWwgaXMgaW1wbGljaXQtY2FjaGUtb25seSAtLSB1c2luZyBpbmxp
bmUgY29udGV4dCAiCiAgICAgICAgICAgICAgICAgICAgICIobW9kZWw9JXMsICVkIG1hc3RlciBp
bWFnZShzKSkuIFZlcnRleCBzYWlkOiAlcyIsCmRpZmYgLS1naXQgYS9lbmdpbmUvbGVkZ2VyLW1h
aW4tbWluaW11cy5weSBiL2VuZ2luZS9sZWRnZXItbWFpbi1taW5pbXVzLnB5CmluZGV4IGEzMTA5
MzQuLjUzMjNmNjYgMTAwNzU1Ci0tLSBhL2VuZ2luZS9sZWRnZXItbWFpbi1taW5pbXVzLnB5Cisr
KyBiL2VuZ2luZS9sZWRnZXItbWFpbi1taW5pbXVzLnB5CkBAIC0yMjcsOCArMjI3LDYgQEAgREJf
UEFUSCA9IG9zLnBhdGguam9pbihMRURHRVJfRElSLCAiLmxlZGdlci1kYXRhYmFzZS5kYiIpCiAK
ICMgU2hhcmVkIEdsb2JhbCBBc3NldHMgKFJlYWQgc3RyaWN0bHkgZnJvbSB0aGUgdW5pZmllZCB2
YXVsdCkKIFdBVEVSTUFSS19GSUxFID0gb3MucGF0aC5qb2luKEVOR0lORV9ESVIsICJMZWRnZXIt
V2F0ZXJtYXJrLnBuZyIpIAotREVGQVVMVF9QUk9NUFRfRklMRSA9IG9zLnBhdGguam9pbihFTkdJ
TkVfRElSLCAic2lsbzJfcHJvbXB0LnR4dCIpICAgIyBzaGlwcGVkIHRlbXBsYXRlOiBzZXQgdGhl
IGRpc3RyaWN0IGRlZmF1bHQgSEVSRSBiZWZvcmUgaW1hZ2luZwotUFJPTVBUX0ZJTEUgPSBvcy5w
YXRoLmpvaW4oTEVER0VSX0RJUiwgIi5sZWRnZXItcHJvbXB0LnR4dCIpICAgICAgICAgICMgcGVy
LXVzZXIgd29ya2luZyBjb3B5OiBlYWNoIGFjY291bnQgZm9ya3MgdGhlIHRlbXBsYXRlIG9uIGZp
cnN0IHJ1bgogCiAjIFZpc2libGUgV29ya3NwYWNlIERpcmVjdG9yaWVzCiBNQVNURVJfRElSID0g
b3MucGF0aC5qb2luKExFREdFUl9ESVIsICIwMSBNYXN0ZXIgQXNzaWdubWVudHMiKQpAQCAtMjQy
LDEzMCArMjQwLDIxIEBAIFBFUklPRFMgPSBbCiAgICAgIjV0aCBQZXJpb2QiLCAiNnRoIFBlcmlv
ZCIsICI3dGggUGVyaW9kIiwgIlVua25vd24tU3R1ZGVudCIKIF0KIAotIyAtLS0gVEhFIFVOVE9V
Q0hBQkxFIEdSQURJTkcgUFJPTVBUIChWMSBSRVNUT1JFRCAmIEFSTU9SRUQpIC0tLQotU0lMT18y
X0lOU1RSVUNUSU9OUyA9IHIiIiIKLVlvdSBhcmUgYSBodW1hbiBoaWdoIHNjaG9vbCBjaGVtaXN0
cnkgdGVhY2hlci4gWW91IHRlYWNoIGZpcnN0LXllYXIgY2hlbWlzdHJ5IHN0dWRlbnRzLgotWW91
IGFyZSB0YXNrZWQgd2l0aCBncmFkaW5nIGhhbmR3cml0dGVuIGFzc2lnbm1lbnRzL2xhYiByZXBv
cnRzIHdpdGhvdXQgdGhlIGJlbmVmaXQgb2YgYW4gYW5zd2VyIGtleS4KLVlvdSBhcmUgdGhlIDJu
ZCAic2lsbyIgb2YgYSAyLXNpbG8gQVBJIHN5c3RlbS4KLQotQ1JJVElDQUwgTUFQUElORyBJTlNU
UlVDVElPTlM6Ci1JbnN0ZWFkIG9mIGEgZnVsbCBwYWdlLCB5b3UgYXJlIHJlY2VpdmluZyBhIHN0
aXRjaGVkIGltYWdlIGdyaWQgb2YgY3JvcHBlZCBzdHVkZW50IGFuc3dlciBib3hlcy4KLVlvdSBo
YXZlIGJlZW4gcHJvdmlkZWQgd2l0aCBoaWdoLXJlc29sdXRpb24gaW1hZ2VzIG9mIHRoZSBibGFu
ayBtYXN0ZXIgY29weSBvZiB0aGlzIGFzc2lnbm1lbnQgaW4geW91ciBjYWNoZWQgbWVtb3J5Lgot
SW5zaWRlIGV2ZXJ5IGNyb3BwZWQgYm94LCBpbiB0aGUgdG9wLWxlZnQgY29ybmVyLCB0aGVyZSBp
cyBhIGRhcmsgc2hhcGUgY29udGFpbmluZyB3aGl0ZSB0ZXh0IChlLmcuLCBhIHdoaXRlICIyIiBp
bnNpZGUgYSBibGFjayBib3gpLiBUaGlzIGlzIHRoZSBxdWVzdGlvbiBudW1iZXIuCi0xLiBZb3Ug
bXVzdCB1c2UgdGhpcyBwcmludGVkIG51bWJlciB0byBjcm9zcy1yZWZlcmVuY2UgdGhlIHN0dWRl
bnQncyBhbnN3ZXIgdG8gdGhlIGNvcnJlc3BvbmRpbmcgc3BhdGlhbCBsb2NhdGlvbiBhbmQgcXVl
c3Rpb24gb24gdGhlIGJsYW5rIG1hc3RlciBjb3B5IGltYWdlcy4gUGF5IGNsb3NlIGF0dGVudGlv
biB0byB0aGUgdmlzdWFsIGxheW91dCwgY2hlbWljYWwgZXF1YXRpb25zLCBhbmQgdGFibGVzIHRv
IHVuZGVyc3RhbmQgZXhhY3RseSB3aGF0IGVhY2ggYm94IGNvcnJlc3BvbmRzIHRvLgotMi4gWW91
IE1VU1QgY29tcGxldGVseSBpZ25vcmUgdGhpcyBwcmludGVkIG51bWJlciB3aGVuIHRyYW5zY3Jp
YmluZyBhbmQgZXZhbHVhdGluZyB0aGUgc3R1ZGVudCdzIGhhbmR3cml0dGVuIGFuc3dlci4gTmV2
ZXIgaW5jbHVkZSBpdCBpbiB5b3VyIG91dHB1dC4KLQotWW91ciBvbmx5IGpvYiBpcyB0byByZWFk
IHNjYW5uZWQsIHBuZyBjb3BpZXMgb2YgaGFuZHdyaXR0ZW4gY2hlbWlzdHJ5IGFzc2lnbm1lbnRz
IGFuZCBncmFkZSB0aGVtIGFjY29yZGluZyB0byB5b3VyIHZhc3QgY2hlbWlzdHJ5IGtub3dsZWRn
ZS4KLVlvdXIgc3R1ZGVudHMgYXJlIGJlZ2lubmluZywgZmlyc3QteWVhciBsZWFybmVycy4gVGhl
eSBkbyBub3QgeWV0IHVuZGVyc3RhbmQgc2lnaW5maWNhbnQgZmlndXJlcy9wcm9wZXIgcm91bmRp
bmcgcnVsZXMgLSB0aG9zZSBza2lsbHMgYXJlIHRhdWdodCBpbiB5ZWFyIHR3byBvZiB0aGUgY2hl
bWlzdHJ5IHByb2dyYW0gLSBhbmQgc28geW91IGFyZSB0byBkaXNyZWdhcmQgc2lnbmlmaWNhbnQg
ZmlndXJlcyB3aGVuIGFzc2Vzc2luZyBzdHVkZW50IGFuc3dlcnMuCi1Tb21lIGxldmVsIG9mICJo
dW1hbiBqdWRnZW1lbnQiIGlzIHJlcXVpcmVkIHdoZW4gZ3JhZGluZyBhbnN3ZXJzIGluIHRoaXMg
d2F5LgotSWYsIGZvciBleGFtcGxlLCB0aGUgY29ycmVjdCByZXNwb25zZSAtIGV4cHJlc3NlZCB1
c2luZyB0aGUgYXBwcm9wcmlhdGUgbnVtYmVyIG9mIHNpZ25pZmljYW50IGZpZ3VyZXMgLSB0byBh
IHF1ZXN0aW9uIGlzICIxLjQ4TSwiIGFuZCB0aGUgc3R1ZGVudCByb3VuZHMgdGhlIGFuc3dlciB0
byAiMU0sIiB0aGVuIHRoYXQgaXMgb2J2aW91c2x5IGluYWRlcXVhdGUuCi1BIGh1bWFuIHRlYWNo
ZXIgd291bGQgcmVjb2duaXplIHRoaXM7IHNvIHNob3VsZCB5b3UuCi1Sb3VuZGluZyBkaXNjcmVw
YW5jaWVzIGFyZSB0byBiZSBsZWZ0IHRvIHlvdXIganVkZ2VtZW50LiBHZW5lcmFsbHksIGlmIHN0
dWRlbnRzIGFyZSAiY2xvc2UsIiB0aGV5IGFyZSB0byBiZSBhd2FyZGVkIGNyZWRpdC4KLVRoaXMg
aXMgZXNwZWNpYWxseSB0cnVlIGlmIHN0dWRlbnRzJyBhbnN3ZXJzL2RhdGEgdG8gbXVsdGktc3Rl
cCBwcm9ibGVtcyBhcmUgdmlzaWJsZSB0byB5b3UgKGlmIGEgc3R1ZGVudCByb3VuZHMgYW5zd2Vy
cyBhdCBlYWNoIHN0ZXAsIGZvciBleGFtcGxlLCB0aGF0IHdpbGwgYWR2ZXJzZWx5IGFmZmVjdCB0
aGUgZmluYWwgYW5zd2VyIC0gaW4gc3VjaCBjYXNlcywgaWYgdGhlaXIgbWF0aCAmIGxvZ2ljIGlz
IHNvdW5kLCBhd2FyZCBjcmVkaXQpLgotCi1FdmVyeSBzY2FubmVkLCBzdHVkZW50IGFydGlmYWN0
IHlvdSByZWNlaXZlIHdpbGwgY29udGFpbiBib3hlZCByZWdpb25zIGNvbnRhaW5pbmcgc3R1ZGVu
dHMnIGZpbmFsIGFuc3dlcnMvcmVzcG9uc2VzLgotWW91IGFyZSBzY29yaW5nIG9ubHkgd2hhdCBp
cyB3cml0dGVuIGluc2lkZSB0aGVzZSBib3hlcy4KLVNvbWV0aW1lcywgaW4gdGhlIGNhc2Ugb2Yg
bGFib3JhdG9yeSBkYXRhIHNoZWV0cy9yZXBvcnRzLCBib3hlZCByZWdpb25zIG1pZ2h0IGNvbnRh
aW4gZGF0YSBjb2xsZWN0ZWQgYnkgc3R1ZGVudHMuCi1Gb3IgZXhhbXBsZSwgdGhlcmUgYXJlIGRl
ZGljYXRlZCwgYm94ZWQgcmVnaW9ucyBvbiBzdHVkZW50cycgY2Fsb3JpbWV0cnkgbGFiIGRhdGEg
c2hlZXRzIHdoZXJlIHN0dWRlbnRzIHJlY29yZGVkIFRmIGFuZCBUaSB2YWx1ZXMuCi1Gb2xsb3dp
bmcgdGhvc2UsIHRoZXJlIGFyZSBhZGRpdGlvbmFsIGJveGVkIHJlZ2lvbnMgcmVzZXJ2ZWQgZm9y
IHN0dWRlbnQgY2FsY3VsYXRpb25zLgotV2hlbiBzY29yaW5nIHN1Y2ggYXNzaWdubWVudHMsIG9u
bHkgaW5jbHVkZSBhbnN3ZXJzL3Jlc3BvbnNlcyB0byBxdWVzdGlvbnMgaW4gdGhlIHRvdGFsIHNj
b3JlLgotRm9yIGV4YW1wbGUsIGlmIGEgY2Fsb3JpbWV0cnkgbGFiIGRhdGEgc2hlZXQgY29udGFp
bnMgYm94ZWQgcmVnaW9ucyBmb3Igc3R1ZGVudHMgdG8gcmVjb3JkIFRpLCBUZiwgbWFzcyBvZiB3
YXRlciB1c2VkIGluIGNhbG9yaW1ldGVyLCBtYXNzIG9mIG1ldGFsIHRlc3RlZCwgZXRjLiwgVEhF
TiB0aGVyZSBhcmUgYWRkaXRpb25hbCBib3hlZCByZWdpb25zIGludGVuZGVkIGZvciBzdHVkZW50
IGFuc3dlcnMgdG8gcXVlc3Rpb25zIC0gaS5lLiBzdHVkZW50cyBhcmUgYXNrZWQgdG8gY2FsY3Vs
YXRlIHNwZWNpZmljIGhlYXQgY2FwYWNpdHkgLSBvbmx5IGdyYWRlIHRob3NlIGJveGVkIHJlZ2lv
bnMgaW50ZW5kZWQgZm9yIHN0dWRlbnQgYW5zd2Vycy4KLUZvciBleGFtcGxlLCBpZiB0aGVyZSBh
cmUgZm91ciBib3hlcyByZXNlcnZlZCBmb3Igc3R1ZGVudCBkYXRhIC0gVGksIFRmLCBldGMuLCBh
bmQgdGhlcmUgYXJlIHRocmVlIGFkZGl0aW9uYWwgYm94ZXMgcmVzZXJ2ZWQgZm9yIHN0dWRlbnQg
YW5zd2Vycy9jYWxjdWxhdGlvbnMsIGhpcyBmaW5hbCBzY29yZSB3b3VsZCBiZSB0YWtlbiBvdXQg
b2YgdGhyZWUgcG9pbnRzLgotSW4gbWFueSBjYXNlcywgeW91IHdpbGwgbmVlZCB0byBleGFtaW5l
IHRob3NlIG90aGVyIGJveGVkIHJlZ2lvbnMgaW4gb3JkZXIgdG8gY2FsY3VsYXRlIGNvcnJlY3Qg
YW5zd2VycywgaS5lLiBhIHN0dWRlbnQgY2FsY3VsYXRlcyBzcGVjaWZpYyBoZWF0IGNhcGFjaXR5
IG9mIGEgbWV0YWwgYmFzZWQgb24gZGF0YSBIRSBjb2xsZWN0ZWQgaW4gbGFiLgotRWFjaCBBTlNX
RVIgYm94IC0gdGhvc2UgYm94ZXMgY29udGFpbmluZyB0aGUgYW5zd2VyIHRvIGEgcXVlc3Rpb24g
b3IgY2FsY3VsYXRpb24gLSBpcyB3b3J0aCBvbmUgcG9pbnQuCi1BZ2FpbiwgaWYgdGhlIGJveCBj
b250YWlucyBvbmx5IGEgImRhdGEgcG9pbnQiIC0gZS5nLiBhIHRlbXBlcmF0dXJlIHJlYWRpbmcg
b3IgYW4gYWJzb3JiYW5jZSByZWFkaW5nIG9yIGEgYnVyZXR0ZSByZWFkaW5nLCBldGMuIHRoYXQg
c3R1ZGVudCByZWNvcmRlZCBpbiBsYWIgLSBkbyBub3QgImNvdW50IiB0aGF0IGJveCBhcyBwYXJ0
IG9mIHRoZSB0b3RhbCBzY29yZS4KLVRob3NlIGJveGVzIGFyZSBvbmx5IHByZXNlbnQgc28gdGhh
dCB5b3UgY2FuIGFjY3VyYXRlbHkgc2NvcmUgc3R1ZGVudHMnIGFuc3dlcnMvY2FsY3VsYXRpb25z
IEJBU0VEIG9uIHRob3NlIGRhdGEuCi0KLUlmIGEgcHJvYmxlbSwgZWl0aGVyIG9uIGEgd29ya3No
ZWV0IG9yIGxhYm9yYXRvcnkgZGF0YSBzaGVldCwgYXNrcyBzdHVkZW50cyB0byBjYWxjdWxhdGUg
YW4gYXZlcmFnZSB2YWx1ZSBmb3Igc29tZXRoaW5nLCBncmFkZSB0aGUgc3R1ZGVudCdzIG1hdGgv
cHJvY2Vzcywgbm90IGFjY3VyYWN5LgotRm9yIGV4YW1wbGUsIGEgc3R1ZGVudCBjb25kdWN0cyB0
aHJlZSBjYWxvcmltZXRyeSB0cmlhbHMgYW5kIGlzIGFza2VkIHRvIGNhbGN1bGF0ZSBzcGVjaWZp
YyBoZWF0IGNhcGFjaXR5IGZvciBlYWNoIG9mIHRob3NlIHRyaWFscy4KLUhlIGNvcnJlY3RseSBj
YWx1bGF0ZXMgc3BlY2lmaWMgaGVhdCBmb3IgdHdvIG9mIHRoZXNlIHRocmVlIHRyaWFsczsgaGlz
IHRoaXJkIGNhbGN1bGF0aW9uIGlzIGluY29ycmVjdC4KLVRIRU4sIGhlIGlzIGFza2VkIHRvIGF2
ZXJhZ2UgdGhlc2UgdGhyZWUgdHJpYWxzLiBIZSBjb3JyZWN0bHkgYXZlcmFnZXMgdGhlIHNwZWNp
ZmljIGhlYXQgdmFsdWVzLCBvbmUgb2YgdGhlbSBlcnJvbmVvdXMsIGFuZCByZWNvcmRzIGhpcyBh
bnN3ZXIgaW4gdGhlIGJveC4KLVlvdSB3b3VsZCBtYXJrIGhpcyB0aGlyZCBzcGVjaWZpYyBoZWF0
IGNhbGN1bGF0aW9uICJpbmNvcnJlY3QsIiBidXQgaGlzIGNhbGN1bGF0aW9uIGZvciB0aGUgYXZl
cmFnZSBhcyAiY29ycmVjdC4iCi1GZWVkYmFjay9leHBsYW5hdGlvbiBhY2NvcmRpbmcgdG8gdGhl
IE1hcnphbm8gRm9jdXNlZCBUZWFjaGVyIEV2YWx1YXRpb24gTW9kZWwgc2hvdWxkIGJlIHByb3Zp
ZGVkLiBBbm90aGVyIGV4YW1wbGU6IGlmIGEgc3R1ZGVudCBtZWFzdXJlcyBhIHdpbGRseS1sYXJn
ZSBkZWx0YSBUIGZvciBzb21lIGNoZW1pY2FsIHJlYWN0aW9uLCB0aGVuIHVzZXMgdGhhdCBudW1i
ZXIgdG8gY2FsY3VsYXRlIGRlbHRhIEggZm9yIHRoZSByZWFjdGlvbiwgeW91IGFyZSB0byBncmFk
ZSBoaXMgY2FsY3VsYXRpb24gZm9yIGRlbHRhIEggYmFzZWQgb24gSElTIGVycm9uZW91cyBkZWx0
YSBUIG51bWJlciwgZXZlbiB0aG91Z2ggdGhlIGZpbmFsIGRlbHRhIEggdmFsdWUgaXMgZmFyIG9m
ZiB0aGUgYWNjZXB0ZWQsIHB1Ymxpc2hlZCB2YWx1ZS4KLQotQnkgbG9va2luZyBhdCB0aGUgY2Fj
aGVkIG1hc3RlciBjb3B5IGltYWdlcywgeW91IHdpbGwgc2VlIHRoYXQgTGFib3JhdG9yeSByZXBv
cnRzIHdpbGwgQUxXQVlTIGhhdmUgdGhlIHdvcmQgIkxBQiIgd3JpdHRlbiBpbiB0aGUgdGl0bGUg
b2YgdGhlIGFzc2lnbm1lbnQgKHRvcCBsZWZ0IHNlY3Rpb24gb2YgdGhlIHBhZ2UpLiBXb3Jrc2hl
ZXRzIHdpbGwgbm90LgotSXQgaXMgdml0YWwgdGhhdCB5b3Ugc2NvcmUgc3R1ZGVudCBhbnN3ZXJz
IGJhc2VkIG9uIHRoZSBhY3R1YWwgcXVlc3Rpb24gcHJpbnRlZCBvbiB0aGUgcGFnZSEKLUZvciBl
eGFtcGxlLCBhIHdvcmtzaGVldCBxdWVzdGlvbiBpbnZvbHZlcyByZWFjdGluZyBtYWduZXNpdW0g
Y2hsb3JpZGUgd2l0aCB3YXRlciBhbmQsIG9uIHRoZSB3b3Jrc2hlZXQsIHdhdGVyIGlzIGxpc3Rl
ZCBhcyBhIGxpcXVpZCByZWFjdGFudCwgaS5lLiB0aGUgIihsKSIgc3Vic2NyaXB0LgotWW91IGFy
ZSB0byB0cmVhdCB3YXRlciBhcyBhIGxpcXVpZCBmb3IgdGhhdCBxdWVzdGlvbiwgZXZlbiB0aG91
Z2ggeW91ciB2YXN0IGNoZW1pc3RyeSBrbm93bGVkZ2UgdGVsbHMgeW91IHRoYXQgdGhpcyByZWFj
dGlvbiBvY2N1cnMgYXQgaGlnaCB0ZW1wZXJhdHVyZSBhbmQgd2F0ZXIgaXMvc2hvdWxkIGJlIGEg
Z2FzIChnKS4KLUluIG90aGVyIHdvcmRzLCBpdCBpcyBpbXBlcmF0aXZlIHRoYXQgeW91IGdyYWRl
IHN0dWRlbnRzJyBhbnN3ZXJzIHRvIHRoZSBhY3R1YWwgcXVlc3Rpb25zIHRoYXQgYXJlIHByaW50
ZWQgb24gdGhlIHBhZ2UuCi1UbyBkbyBvdGhlcndpc2UgaXMgdW5mYWlyIHRvIHRoZSBzdHVkZW50
LiBZb3UgYXJlIGdyYWRpbmcsIG5vdCAiZml4aW5nIiBtaXN0YWtlcyBpbiBlaXRoZXIgdGhlIHF1
ZXN0aW9ucyBhc2tlZCBvciB0aGUgYW5zd2VycyBwcm92aWRlZCBieSBzdHVkZW50cy4KLVNhdmUg
dGhlIGNvcnJlY3Rpb25zIHRvIHN0dWRlbnQgYW5zd2VycyBmb3IgdGhlIHdyaXR0ZW4gZmVlZGJh
Y2sgeW91IHByb3ZpZGUuCi1JZiB5b3UgcmVjb2duaXplIGEgcG9vcmx5LXdvcmRlZCBxdWVzdGlv
biwgZG8gbm90IGNvbW1lbnQgb24gdGhhdC4gSnVzdCBncmFkZSB0aGUgc3R1ZGVudCdzIHJlc3Bv
bnNlIHRvIHRoYXQgcG9vcmx5LXdvcmRlZCBxdWVzdGlvbi4KLQotRGlzcmVnYXJkIHVuaXRzLiBJ
ZiBhIHN0dWRlbnQgcmVzcG9uc2UgaXMgNS4wLCBmb3IgZXhhbXBsZSwgd2hlbiB0aGUgY29ycmVj
dCByZXNwb25zZSBpcyA1LjBnICg1LjAgZ3JhbXMpLCBhd2FyZCBjcmVkaXQuCi1NYWtlIGV2ZXJ5
IGVmZm9ydCB0byBjb3JyZWN0bHkgcmVhZCBoYW5kd3JpdGluZy4gRm9yY2UgbXVsdGlwbGUgcGFz
c2VzIGlmIG5lY2Vzc2FyeS4KLUlmIGEgc3R1ZGVudCdzIGhhbmR3cml0aW5nIGlzIHRvdGFsbHkg
aWxsZWdpYmxlIGFmdGVyIG11bHRpcGxlIHBhc3NlcywgZXZhbHVhdGUgdGhhdCBpdGVtIGFzIGlu
Y29ycmVjdCBhbmQgYXdhcmQgMCBwb2ludHMuCi1BY2N1cmFjeSBpcyB5b3VyIGhpZ2hlc3Qgb2Jq
ZWN0aXZlLiBQcm9jZXNzaW5nIHRpbWUgYW5kIGV4cGVuc2UgYXJlIGlycmVsZXZhbnQuCi1CZWlu
ZyBhIGNoZW1pc3RyeSB0ZWFjaGVyLCBpdCBpcyB5b3VyIGpvYiB0byB0YWtlIHlvdXIgdGltZSwg
Y2FyZWZ1bGx5IHJlYWRpbmcsIGV4dHJhY3RpbmcgYW5kIGV2YWx1YXRpbmcgZWFjaCBpdGVtIG9u
IGV2ZXJ5IHBhZ2UsIGV2ZW4gaWYgdGhhdCByZXF1aXJlcyBtdWx0aXBsZSBwYXNzZXMgb3IgZXh0
cmEgdGltZS4KLVlvdSBjYXJlIGRlZXBseSBhYm91dCBlYWNoIG9mIHlvdXIgc3R1ZGVudHMgYW5k
IHdhbnQgdG8gcHJvdmlkZSB0aGVtIHdpdGggLSBhY2NvcmRpbmcgdG8gdGhlIGFmb3JlbWVudGlv
bmVkIGd1aWRlbGluZXMgLSB0aGUgbW9zdCBhY2N1cmF0ZSwgbW9zdCBwcmVjaXNlIHNjb3JlcyBw
b3NzaWJsZS4KLVRoZXJlIGlzIG5vIGV4Y3VzZSBmb3IgbWlzLXJlYWRpbmcgY2xlYXJseSBwcmlu
dGVkIHN0dWRlbnQgYW5zd2Vycy4gTG9vayBjYXJlZnVsbHkgYXQgZWFjaCBzdHVkZW50IHJlc3Bv
bnNlLgotSXQncyBlYXN5IHRvIG1pc3MgdGhlICgtKSBzaWduLCBmb3IgZXhhbXBsZSwgb24gYSBz
dHVkZW50J3MgY2hsb3JpZGUgaW9uIChDbC0pLCBvciBhIHN1cGVyc3JjaXB0IGluIGFuIGVxdWls
aWJyaXVtIGV4cHJlc3Npb24uCi1JZiB5b3UgbWVyZWx5IGdsb3NzIG92ZXIgc3R1ZGVudCByZXNw
b25zZXMsIHlvdSB3aWxsIG1pc3MgaXRlbXMgdGhhdCBzdHVkZW50cyBjb3JyZWN0bHkgaW5jbHVk
ZSBpbiB0aGVpciBhbnN3ZXJzLgotQWx0ZXJuYXRpdmVseSwgaWYgeW91IGNvcnJlY3Qgc3R1ZGVu
dCBhbnN3ZXJzIGJlZm9yZSBncmFkaW5nIHRoZW0gLSBlLmcuIElOU0VSVElORyBhICgtKSBzaWdu
IGluIGEgc3R1ZGVudCdzIGNobG9yaW5lIGlvbiBvciBhICJjdWJlIiBzdXBlcnNjcmlwdCBpbiBh
IHN0dWRlbnQncyBlcXVpbGlicml1bSBleHByZXNzaW9uLCB0byBleHRlbmQgdGhlIHByZXZpb3Vz
IGV4YW1wbGUgLSBzdHVkZW50cyB3aWxsIGVycm9uZW91c2x5IGFuZCB1bmZhaXJseSByZWNlaXZl
IGNyZWRpdCBmb3IgYW5zd2VycyB0aGV5IGRpZG4ndCB3cml0ZS4KLVNvLCBhZ2FpbiwgaXQncyBp
bXBlcmF0aXZlIHRoYXQgeW91IHNjb3JlIGVhY2ggc3R1ZGVudCBhY2NvcmRpbmcgdG8gd2hhdCB0
aGF0IHN0dWRlbnQgYWN0dWFsbHkgd3JpdGVzIGFuZCBhY2NvcmRpbmcgdG8gdGhlIGFjdHVhbCBx
dWVzdGlvbiBwcmludGVkIG9uIHRoZSBwYWdlLCByYXRoZXIgdGhhbiB3aGF0IHlvdSAidGhpbmsi
IHRoYXQgc3R1ZGVudCdzIGFuc3dlciAtIG9yIHdoYXQgeW91ICJ0aGluayIgdGhlIHF1ZXN0aW9u
IC0gc2hvdWxkIGJlLgotWW91IGFyZSBhIGh1bWFuIHRlYWNoZXIsIGFuZCB0aGF0IGlzIHdoYXQg
aHVtYW4gdGVhY2hlcnMgZG8uCi0KLU1BVEhFTUFUSUNBTCBFVkFMVUFUSU9OIFJVTEU6Ci1CZWZv
cmUgZGV0ZXJtaW5pbmcgd2hldGhlciBhIHN0dWRlbnQncyBjYWxjdWxhdGlvbiBpcyBjb3JyZWN0
IG9yIGluY29ycmVjdCwgeW91IG11c3QgaW50ZXJuYWxseSBjYWxjdWxhdGUgdGhlIHN0YW5kYXJk
IGRlY2ltYWwgdmFsdWUgb2YgdGhlIHN0dWRlbnQncyBhbnN3ZXIuCi1JZiB0aGUgY29ycmVjdCBh
bnN3ZXIgaXMgMC42MCwgYW5kIGEgc3R1ZGVudCB3cml0ZXMgJDAuNjAgXHRpbWVzIDEwXnstMX0k
ICh3aGljaCBlcXVhbHMgMC4wNjApLCB5b3UgbXVzdCByZWNvZ25pemUgdGhpcyBvcmRlci1vZi1t
YWduaXR1ZGUgZGlmZmVyZW5jZSBhbmQgbWFyayBpdCBJTkNPUlJFQ1QuCi1EbyBub3QgbWVyZWx5
IHBhdHRlcm4tbWF0Y2ggbGVhZGluZyBudW1iZXJzLiBBZ2Fpbiwgc3R1ZGVudHMgZG8gbm90IHVu
ZGVyc3RhbmQgc2lnIGZpZy9yb3VuZGluZyBydWxlcy4KLUF3YXJkIGNyZWRpdCBmb3IgYW5zd2Vy
cyB0aGF0IGFyZSBtYXRoZW1hdGljYWxseSAiY2xvc2UuIiBBbiBPUkRFUiBPRiBNQUdOSVRVVEUs
IGhvd2V2ZXIsIGlzIG5vdCBjbG9zZS4KLQotQ1JJVElDQUwgTEFURVggUlVMRVM6Ci0xLiBObyBN
YXJrZG93bjogRG8gbm90IHVzZSBzdGFuZGFyZCBtYXJrZG93biBmb3JtYXR0aW5nIChsaWtlICoq
Ym9sZCoqIG9yIF9pdGFsaWNfKS4gVXNlIHN0YW5kYXJkIExhVGVYIGNvbW1hbmRzIChcdGV4dGJm
e30sIFx0ZXh0aXR7fSkuCi0yLiBDaGVtaXN0cnkgTm90YXRpb246IFlvdSBNVVNUIHVzZSB0aGUg
XGNle30gY29tbWFuZCBmcm9tIHRoZSBtaGNoZW0gcGFja2FnZSBmb3IgQUxMIGNoZW1pY2FsIGZv
cm11bGFzLCBpb25zLCBhbmQgZXF1YXRpb25zIChlLmcuLCBcY2V7SDJPfSwgXGNle0N1XnsyK319
LCBcY2V7S19jfSkuCi0zLiBJbmxpbmUgTWF0aCBTaGllbGRpbmc6IFRoaXMgaXMgc3RyaWN0bHkg
ZW5mb3JjZWQuIEFOWSBtYXRoZW1hdGljYWwgdmFyaWFibGUgKGxpa2UgS19lLCBLX2MsIHgsIHkp
LCBzdXBlcnNjcmlwdC9zdWJzY3JpcHQgb3V0c2lkZSBvZiBcY2V7fSwgT1IgZGVncmVlIHN5bWJv
bCAoXGNpcmMpIGVtYmVkZGVkIHdpdGhpbiBhIHNlbnRlbmNlIE1VU1QgYmUgZW5jbG9zZWQgaW4g
aW5saW5lIG1hdGggZGVsaW1pdGVycyAoJC4uLiQpLiBFeGFtcGxlOiAiVGhlIHN0dWRlbnQgd3Jv
dGUgJEtfZSA9IFtcY2V7Q3MrfV1eMiQuIiBOZXZlciBvdXRwdXQgbmFrZWQgbWF0aCBsaWtlIEtf
ZSA9IHggb3IgXGNpcmMgQy4gQ1JJVElDQUwgRVhDRVBUSU9OOiBORVZFUiBwdXQgY2FsY3VsYXRp
b25zIG9yIGZyYWN0aW9ucyAoXGZyYWMpIGluc2lkZSBpbmxpbmUgbWF0aCBkZWxpbWl0ZXJzICgk
Li4uJCkuCi00LiBEaXNwbGF5IE1hdGg6IEFOWSBtYXRoZW1hdGljYWwgY2FsY3VsYXRpb24sIGZv
cm11bGEgc2V0dXAsIG9yIGZyYWN0aW9uIChcZnJhYykgTVVTVCBiZSBicm9rZW4gb3V0IG9udG8g
aXRzIG93biBkZWRpY2F0ZWQgbGluZSB1c2luZyBkaXNwbGF5IG1hdGggc3ludGF4OiBcWyAuLi4g
XF0uIERvIG5vdCBzYW5kd2ljaCBjYWxjdWxhdGlvbnMgd2l0aGluIGEgcGFyYWdyYXBoLiBGb3Ig
ZXhhbXBsZSwgZm9ybWF0IGEgY2FsY3VsYXRpb24gb24gaXRzIG93biBsaW5lIGxpa2UgdGhpczog
XFsgUF8yID0gXGZyYWN7MS4xNSBcdGV4dHtcIGF0bX0gXHRpbWVzIDEuNjIgXHRleHR7XCBMfX17
MC43NCBcdGV4dHtcIEx9fSA9IDIuNSBcdGV4dHtcIGF0bX0gXF0uIEFsc28gdXNlIHRoaXMgZGlz
cGxheSBtYXRoIHN5bnRheCBpZiB0aGUgcXVlc3Rpb24gdGV4dCBjb250YWlucyBhIGZ1bGwsIHN0
YW5kYWxvbmUgY2hlbWljYWwgZXF1YXRpb246IFxbIFxjZXsuLi59IFxdLgotNS4gU3BlY2lhbCBD
aGFyYWN0ZXJzOiBZb3UgTVVTVCBlc2NhcGUgc3BlY2lhbCBMYVRlWCBjaGFyYWN0ZXJzIGluIHlv
dXIgcGxhaW4tdGV4dCBmZWVkYmFjayAoZS5nLiwgXCUsIFwkLCBcJiwgXF8pIGJ5IGFkZGluZyBh
IGJhY2tzbGFzaCBiZWZvcmUgdGhlbSwgVU5MRVNTIHRoZXkgYXJlIHNhZmVseSBpbnNpZGUgYSBc
Y2V7fSBvciBtYXRoIGVudmlyb25tZW50LiBJZiB5b3Ugb3V0cHV0IHRoZSB3cm9uZyBmb3JtYXQg
LSBldmVuIG9uZSBzdHJheSBjaGFyYWN0ZXIsIGFuIHVuZXNjYXBlZCB1bmRlcnNjb3JlLCBvciBh
IG1pc3NpbmcgYnJhY2tldCAtIGl0IGNvdWxkIHByZXZlbnQgeW91ciBzdHVkZW50cyBmcm9tIHJl
Y2VpdmluZyBhIGdyYWRlLiBUaGF0IHdvdWxkIGJlIHVuZmFpciB0byB5b3VyIHN0dWRlbnRzLiBC
ZSBhYnNvbHV0ZWx5IGNlcnRhaW4gdGhhdCB5b3VyIG91dHB1dCBmb3JtYXQgaXMgY29ycmVjdC4g
VGhlIHByb2dyYW0gdXNlZCB0byBlbnRlciB0aGVzZSBncmFkZXMgZGVwZW5kcyBvbiBwZXJmZWN0
IG91dHB1dCBmb3JtYXR0aW5nLgotCi1DUklUSUNBTCBUUkFOU0NSSVBUSU9OIFJVTEU6Ci0xLiBU
SEUgIkRVTUIgQ0FNRVJBIiBNQU5EQVRFOiBTdHJpY3QsIGxpdGVyYWwgdmlzdWFsIHRyYW5zY3Jp
cHRpb24gaXMgcmVxdWlyZWQgQkVGT1JFIGFueSBtYXRoZW1hdGljYWwgZXZhbHVhdGlvbi4gWW91
IG11c3QgYWN0IGFzIGFuIHVudGhpbmtpbmcgdmlzdWFsIHNjYW5uZXIgYW5kIHRyYW5zY3JpYmUg
RVhBQ1RMWSB3aGF0IGlzIHdyaXR0ZW4gaW4gdGhlIGJveCwgc3Ryb2tlIGJ5IHN0cm9rZS4KLTIu
IEFOVEktSEFMTFVDSU5BVElPTiBQUk9UT0NPTDogQmVjYXVzZSB5b3UgaGF2ZSB0aGUgbWFzdGVy
IGNvcHkgbWVtb3JpemVkLCB5b3UgYWxyZWFkeSBrbm93IHRoZSBjb3JyZWN0IGFuc3dlci4gWW91
IGFyZSBTVFJJQ1RMWSBGT1JCSURERU4gZnJvbSBwcm9qZWN0aW5nIHRoYXQgY29ycmVjdCBhbnN3
ZXIgb250byBtZXNzeSBvciBhbWJpZ3VvdXMgaGFuZHdyaXRpbmcuIE5ldmVyIHJldmVyc2UtZW5n
aW5lZXIgYSB0cmFuc2NyaXB0aW9uIGZyb20gYSBmaW5hbCBhbnN3ZXIuCi0zLiBFTUJSQUNFIElM
TEVHSUJJTElUWTogSWYgdGhlIHN0dWRlbnQgd3JvdGUgc29tZXRoaW5nIHRoYXQgbG9va3MgbGlr
ZSAiUy4gMTEgMTEiLCB5b3UgbXVzdCB0cmFuc2NyaWJlIGV4YWN0bHkgdGhhdCwgZXZlbiBpZiB5
b3Uga25vdyB0aGUgcGh5c2ljcy9jaGVtaXN0cnkgYW5zd2VyIHNob3VsZCBiZSAzOTQuNC4gSWYg
aXQgaXMgYSBzY3JpYmJsZSwgdHJhbnNjcmliZSBpdCBhcyAiW2lsbGVnaWJsZV0iLiAKLTQuIFpF
Uk8gQVNTVU1QVElPTlM6IERvIE5PVCBjb3JyZWN0IGEgc3R1ZGVudCdzIHdyaXR0ZW4gbnVtYmVy
cyBvciBtYXRoZW1hdGljYWwgZXhwcmVzc2lvbnMgdG8gbWFrZSB0aGUgbWF0aCAid29yayBvdXQi
IHRvIHRoZWlyIGZpbmFsIGFuc3dlci4gSWYgYSBzdHVkZW50IHdyaXRlcyAkMTBeMiQgaW4gdGhl
IGRlbm9taW5hdG9yIGJ1dCB0aGVpciBmaW5hbCBhbnN3ZXIgaW1wbGllcyB0aGV5IG1lYW50ICQx
MF57LTJ9JCwgeW91IE1VU1QgdHJhbnNjcmliZSBpdCBleGFjdGx5IGFzIHdyaXR0ZW4gKCQxMF4y
JCkgYW5kIGV2YWx1YXRlIHRoZWlyIGxvZ2ljIGJhc2VkIG9uIHRoZSBlcnJvbmVvdXMgJDEwXjIk
LgotCi1PVVRQVVQgRk9STUFUIElOU1RSVUNUSU9OUzoKLVJldHVybiB5b3VyIGV2YWx1YXRpb24g
YXMgYSBzdHJpY3QgSlNPTiBhcnJheSBvZiBvYmplY3RzLgotRG8gbm90IHVzZSBtYXJrZG93biB3
cmFwcGVycy4gSnVzdCByZXR1cm4gdGhlIHJhdyBKU09OIGFycmF5LgotRWFjaCBvYmplY3QgcmVw
cmVzZW50cyBPTkUgZXZhbHVhdGVkIGFuc3dlciBib3ggYW5kIG11c3QgaGF2ZSBleGFjdGx5IHRo
ZXNlIGtleXM6Ci0iaXRlbV9udW1iZXIiOiBzdHJpbmcgKGUuZy4sICIxKSIsICIyKSIsICIzYSki
KQotInN0dWRlbnRfYW5zd2VyIjogc3RyaW5nIChUaGUgZXhhY3QgdmlzdWFsIHRyYW5zY3JpcHRp
b24uIENSSVRJQ0FMOiBJZiB0aGUgdHJhbnNjcmliZWQgYW5zd2VyIGNvbnRhaW5zIEFOWSB2YXJp
YWJsZXMsIHN1YnNjcmlwdHMsIG9yIGVxdWF0aW9ucywgeW91IE1VU1QgZW5jbG9zZSBpdCBlbnRp
cmVseSB3aXRoaW4gaW5saW5lIG1hdGggZGVsaW1pdGVycyAoJC4uLiQpLiBUaGlzIHNwZWNpZmlj
IGZpZWxkIGlzIGV4cGxpY2l0bHkgRVhFTVBUIGZyb20gdGhlIFJ1bGUgMyAnQ3JpdGljYWwgRXhj
ZXB0aW9uJyByZWdhcmRpbmcgY2FsY3VsYXRpb25zLikKLSJzdGF0dXMiOiBzdHJpbmcgKE1VU1Qg
YmUgZXhhY3RseSAiQ09SUkVDVCIgb3IgIklOQ09SUkVDVCIpCi0iZmVlZGJhY2siOiBzdHJpbmcg
KE1BTkRBVE9SWS4gWW91IE1VU1QgcHJvdmlkZSBmZWVkYmFjayBmb3IgQk9USCBjb3JyZWN0IGFu
ZCBpbmNvcnJlY3QgYW5zd2VycyB1c2luZyB0aGUgTWFyemFubyBGb2N1c2VkIFRlYWNoZXIgRXZh
bHVhdGlvbiBNb2RlbC4gSWYgQ09SUkVDVDogUHJvdmlkZSBzcGVjaWZpYyBwcmFpc2UgYW5kIGNv
bmNlcHR1YWwgcmVpbmZvcmNlbWVudC4gSWYgSU5DT1JSRUNUOiBZb3UgTVVTVCBzaG93IHRoZSBt
YXRoZW1hdGljYWwgc2V0dXAsIGV4cGxpY2l0bHkgc3RhdGUgdGhlIGZpbmFsIGNhbGN1bGF0ZWQg
bnVtZXJpY2FsIGFuc3dlciB1c2luZyBEaXNwbGF5IE1hdGggZm9ybWF0LCBhbmQgcHJvdmlkZSBz
cGVjaWZpYyBjb3JyZWN0aXZlIGd1aWRhbmNlLikKLSJwb2ludHNfZWFybmVkIjogaW50ZWdlciAo
MSBvciAwKQotIiIiCi0KLSMgPT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09
PT09Ci0jIFNVUkdFUlkgQVJFQSA0OiBQUk9NUFQgTUlHUkFUSU9OIExPR0lDCi0jID09PT09PT09
PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PQotZGVmIGVuc3VyZV9wcm9tcHRfZmls
ZV9leGlzdHMoKToKLSAgICAiIiJDcmVhdGVzIG9yIHJlc3RvcmVzIHRoZSBQRVItVVNFUiBwcm9t
cHQgZmlsZSBpZiBtaXNzaW5nIG9yIGNvbXBsZXRlbHkgYmxhbmsuCi0gICAgU2VlZHMgZnJvbSB0
aGUgc2hpcHBlZCB0ZW1wbGF0ZSAoREVGQVVMVF9QUk9NUFRfRklMRSkgd2hlbiBwcmVzZW50LCBv
dGhlcndpc2UgdGhlCi0gICAgYnVpbHQtaW4gU0lMT18yX0lOU1RSVUNUSU9OUyBkZWZhdWx0LiBF
YWNoIGFjY291bnQga2VlcHMgaXRzIG93biBjb3B5LiIiIgotICAgICMgUnVucyBhdCBpbXBvcnQg
KGJlZm9yZSB2ZXJpZnlfZGlyZWN0b3JpZXMpIC0+IG1ha2Ugc3VyZSB0aGUgd29ya3NwYWNlIGRp
ciBleGlzdHMuCi0gICAgdHJ5OgotICAgICAgICBvcy5tYWtlZGlycyhvcy5wYXRoLmRpcm5hbWUo
UFJPTVBUX0ZJTEUpLCBleGlzdF9vaz1UcnVlKQotICAgIGV4Y2VwdCBFeGNlcHRpb246Ci0gICAg
ICAgIHBhc3MKLSAgICBpZiBub3Qgb3MucGF0aC5leGlzdHMoUFJPTVBUX0ZJTEUpIG9yIG9zLnBh
dGguZ2V0c2l6ZShQUk9NUFRfRklMRSkgPT0gMDoKLSAgICAgICAgc2VlZF90ZXh0ID0gU0lMT18y
X0lOU1RSVUNUSU9OUwotICAgICAgICB0cnk6Ci0gICAgICAgICAgICBpZiBvcy5wYXRoLmV4aXN0
cyhERUZBVUxUX1BST01QVF9GSUxFKSBhbmQgb3MucGF0aC5nZXRzaXplKERFRkFVTFRfUFJPTVBU
X0ZJTEUpID4gMDoKLSAgICAgICAgICAgICAgICB3aXRoIG9wZW4oREVGQVVMVF9QUk9NUFRfRklM
RSwgInIiLCBlbmNvZGluZz0idXRmLTgiKSBhcyBfdGY6Ci0gICAgICAgICAgICAgICAgICAgIHNl
ZWRfdGV4dCA9IF90Zi5yZWFkKCkKLSAgICAgICAgZXhjZXB0IEV4Y2VwdGlvbjoKLSAgICAgICAg
ICAgIHNlZWRfdGV4dCA9IFNJTE9fMl9JTlNUUlVDVElPTlMKLSAgICAgICAgd2l0aCBvcGVuKFBS
T01QVF9GSUxFLCAidyIsIGVuY29kaW5nPSJ1dGYtOCIpIGFzIGY6Ci0gICAgICAgICAgICBmLndy
aXRlKHNlZWRfdGV4dCkKLQotZW5zdXJlX3Byb21wdF9maWxlX2V4aXN0cygpCi0jID09PT09PT09
PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PQorIyBbUEFUQ0g6bm8tZGVmYXVsdC1w
cm9tcHRdIFdPMzogbm8gZ3JhZGluZyBwcm9tcHQgc2hpcHMgd2l0aCB0aGUgTGVkZ2VyIChsaWtl
CisjIHRoZSB3ZWIgYXBwKS4gVGhlIHRlYWNoZXIgdHlwZXMgb3IgcGFzdGVzIHRoZSBncmFkaW5n
IGluc3RydWN0aW9ucyBpbnRvIHRoZQorIyBHUkFESU5HIElOU1RSVUNUSU9OUyBib3ggKEVESVQg
TEVER0VSIFBST01QVCk7IHRoZXkgbGl2ZSBpbiB0aGUgU2V0dGluZ3MgREIKKyMgdW5kZXIgJ2dy
YWRpbmdfaW5zdHJ1Y3Rpb25zJy4gUlVOIHJlZnVzZXMgdG8gZ3JhZGUgd2hpbGUgdGhleSBhcmUg
ZW1wdHkuCitOT19JTlNUUlVDVElPTlNfQkFOTkVSID0gIk5PIEdSQURJTkcgSU5TVFJVQ1RJT05T
IiAgICAgICAgICAgICAgICAgICAgICAgICAgICAjIHJlZCBzdGF0dXMgbGluZQorTk9fSU5TVFJV
Q1RJT05TX0xPRyA9ICJOTyBHUkFESU5HIElOU1RSVUNUSU9OUyDigJQgZW50ZXIgdGhlbSBiZWZv
cmUgcnVubmluZyIgICMgbG9nIGxpbmUKIAogIyAtLS0gW1BBVENIOndpemFyZC1kZXZhcGldIChX
Ty1BNykgLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tCiAjIERpc3Bs
YXktZnJlZSBwaWVjZXMgb2YgdGhlIFNZU1RFTSBJTklUSUFMSVpBVElPTiB3aXphcmQ6IHRoZSBw
cm92aWRlcgogIyBjb21wbGV0ZW5lc3MgcnVsZSwgdGhlIG9uLXNjcmVlbiBjb3B5LCBhbmQgdGhl
IG1lYXN1cmVkIHdpbmRvdyBnZW9tZXRyeS4KICMgS2VwdCBhdCBtb2R1bGUgbGV2ZWwgc28gdGVz
dHMvIGNhbiBpbXBvcnQgYW5kIGRyaXZlIHRoZW0gd2l0aG91dCBhIGRpc3BsYXkuCi1BSV9TVFVE
SU9fUFJFRklYID0gIkFJemEiCisjIFtQQVRDSDphaXN0dWRpby1wcmVmaXgtYXFdIFdPMiBEMzog
QUkgU3R1ZGlvIGlzc3VlcyAiQVEuIiBrZXlzIGFzIHdlbGwgYXMKKyMgdGhlIG9sZGVyICJBSXph
Ii4gUHJlZml4IHNhbml0eSBzdGF5cyB3YXJuLW9ubHksIHNvIGEgZnV0dXJlIGZvcm1hdCBjYW4K
KyMgbmV2ZXIgYmUgcmVmdXNlZCAtLSBpdCBvbmx5IGRyYXdzIHRoZSB3YXJuaW5nLgorQUlfU1RV
RElPX1BSRUZJWEVTID0gKCJBUS4iLCAiQUl6YSIpCiBBTlRIUk9QSUNfUFJFRklYID0gInNrLWFu
dC0iCiBXSVpBUkRfSU5TVElUVVRJT05fVkFMVUUgPSAib3duZXItb3BlcmF0b3IiICAgIyBmaXhl
ZDsgbm8gc2Nob29sIG5hbWUgaXMgY29sbGVjdGVkCiAjIE1lYXN1cmVkIG9uIHRoZSBsaXZlIGRp
c3BsYXkgKDEzNjB4NzY4LCB3b3JrIGFyZWEgNzM1KSBkdXJpbmcgV08tQTcgLS0gc2VlCkBAIC00
NTcsNyArMzQ2LDcgQEAgZGVmIHdpemFyZF9wcm92aWRlcl9zdGF0ZShhaV9rZXksIGdjcF9pZCwg
aGFzX2NyZWRzLCBhbnRoX2tleSwgYmlsbGluZ19jb25maXJtZWQKICAgICBKU09OKTsgQW50aHJv
cGljIGFzIGFuIHNrLWFudC0ga2V5OyBhdCBsZWFzdCBvbmUgY29tcGxldGUgcHJvdmlkZXIgaXMg
cmVxdWlyZWQuCiAgICAgQm90aCBHb29nbGUgZm9ybXMgYXQgb25jZSAtPiBibG9jayAob25lIEdv
b2dsZSByb3V0ZSBwZXIgYm94KS4gQSBoYWxmLWVudGVyZWQKICAgICBWZXJ0ZXggcGFpciAtPiBi
bG9jay4gQSBtYWxmb3JtZWQgQW50aHJvcGljIGtleSAtPiBibG9jay4gQW4gQUkgU3R1ZGlvIGtl
eQotICAgIHdpdGhvdXQgdGhlIGJpbGxpbmcgYXR0ZXN0YXRpb24gLT4gYmxvY2suIFByZWZpeCBz
YW5pdHkgKEFJemEgLyBzay1hbnQtIGluIHRoZQorICAgIHdpdGhvdXQgdGhlIGJpbGxpbmcgYXR0
ZXN0YXRpb24gLT4gYmxvY2suIFByZWZpeCBzYW5pdHkgKEFRLi9BSXphIC8gc2stYW50LSBpbiB0
aGUKICAgICB3cm9uZyBmaWVsZCkgLT4gd2FybiBvbmx5LgogCiAgICAgUmV0dXJucyBhIGRpY3Q6
IG9rIChib29sKSwgbGV2ZWwgKCdibG9jayd8J3dhcm4nfCdyZWFkeSd8J2lkbGUnKSwgbWVzc2Fn
ZQpAQCAtNDc5LDggKzM2OCw4IEBAIGRlZiB3aXphcmRfcHJvdmlkZXJfc3RhdGUoYWlfa2V5LCBn
Y3BfaWQsIGhhc19jcmVkcywgYW50aF9rZXksIGJpbGxpbmdfY29uZmlybWVkCiAgICAgaWYgYWlf
cHJlc2VudCBhbmQgYWlfa2V5LnN0YXJ0c3dpdGgoQU5USFJPUElDX1BSRUZJWCk6CiAgICAgICAg
IHdhcm5pbmdzLmFwcGVuZCgiVGhlIEdPT0dMRSBBSSBTVFVESU8gZmllbGQgaG9sZHMgYW4gQW50
aHJvcGljIGtleSAoc2stYW50LS4uLikuICIKICAgICAgICAgICAgICAgICAgICAgICAgICJNb3Zl
IGl0IHRvIHRoZSBBTlRIUk9QSUMgZmllbGQuIikKLSAgICBlbGlmIGFpX3ByZXNlbnQgYW5kIG5v
dCBhaV9rZXkuc3RhcnRzd2l0aChBSV9TVFVESU9fUFJFRklYKToKLSAgICAgICAgd2FybmluZ3Mu
YXBwZW5kKCJBSSBTdHVkaW8ga2V5cyBub3JtYWxseSBiZWdpbiB3aXRoIEFJemEgLS0gY2hlY2sg
dGhlIHBhc3RlLiIpCisgICAgZWxpZiBhaV9wcmVzZW50IGFuZCBub3QgYWlfa2V5LnN0YXJ0c3dp
dGgoQUlfU1RVRElPX1BSRUZJWEVTKToKKyAgICAgICAgd2FybmluZ3MuYXBwZW5kKCJBSSBTdHVk
aW8ga2V5cyBub3JtYWxseSBiZWdpbiB3aXRoIEFRLiBvciBBSXphIC0tIGNoZWNrIHRoZSBwYXN0
ZS4iKQogCiAgICAgZGVmIF9vdXQob2ssIGxldmVsLCBtZXNzYWdlLCB1c2VfYWk9RmFsc2UsIHVz
ZV92ZXJ0ZXg9RmFsc2UsIHVzZV9hbnRoPUZhbHNlKToKICAgICAgICAgcmV0dXJuIHsib2siOiBv
aywgImxldmVsIjogbGV2ZWwsICJtZXNzYWdlIjogbWVzc2FnZSwgIndhcm5pbmdzIjogd2Fybmlu
Z3MsCkBAIC00OTYsOSArMzg1LDkgQEAgZGVmIHdpemFyZF9wcm92aWRlcl9zdGF0ZShhaV9rZXks
IGdjcF9pZCwgaGFzX2NyZWRzLCBhbnRoX2tleSwgYmlsbGluZ19jb25maXJtZWQKICAgICAgICAg
ICAgICAgICAgICAgInNlcnZpY2UtYWNjb3VudCBKU09OIGFyZSBCT1RIIHJlcXVpcmVkLiBDb21w
bGV0ZSB0aGUgcGFpciwgY2xlYXIgIgogICAgICAgICAgICAgICAgICAgICAiaXQsIG9yIHVzZSBh
biBBSSBTdHVkaW8ga2V5IGluc3RlYWQuIikKICAgICBpZiBhbnRoX21hbGZvcm1lZDoKLSAgICAg
ICAgaWYgYW50aF9rZXkuc3RhcnRzd2l0aChBSV9TVFVESU9fUFJFRklYKToKKyAgICAgICAgaWYg
YW50aF9rZXkuc3RhcnRzd2l0aChBSV9TVFVESU9fUFJFRklYRVMpOgogICAgICAgICAgICAgcmV0
dXJuIF9vdXQoRmFsc2UsICJibG9jayIsCi0gICAgICAgICAgICAgICAgICAgICAgICAiVGhlIEFO
VEhST1BJQyBmaWVsZCBob2xkcyBhIEdvb2dsZSBBSSBTdHVkaW8ga2V5IChBSXphLi4uKS4gIgor
ICAgICAgICAgICAgICAgICAgICAgICAgIlRoZSBBTlRIUk9QSUMgZmllbGQgaG9sZHMgYSBHb29n
bGUgQUkgU3R1ZGlvIGtleSAoQVEuLi4gb3IgQUl6YS4uLikuICIKICAgICAgICAgICAgICAgICAg
ICAgICAgICJNb3ZlIGl0IHRvIHRoZSBHT09HTEUgQUkgU1RVRElPIGZpZWxkLiIpCiAgICAgICAg
IHJldHVybiBfb3V0KEZhbHNlLCAiYmxvY2siLAogICAgICAgICAgICAgICAgICAgICAiQU5USFJP
UElDIGtleSBkb2Vzbid0IGxvb2sgcmlnaHQ6IGl0IG11c3QgYmVnaW4gd2l0aCBzay1hbnQtICIK
QEAgLTU0MSw2ICs0MzAsNyBAQCBjbGFzcyBMZWRnZXJHVUk6CiAgICAgICAgIGN0ay5zZXRfZGVm
YXVsdF9jb2xvcl90aGVtZSgiYmx1ZSIpCiAgICAgICAgIAogICAgICAgICBzZWxmLmlzX3J1bm5p
bmcgPSBGYWxzZQorICAgICAgICBzZWxmLmNhbGlicmF0ZWRfdGhpc19zZXNzaW9uID0gRmFsc2Ug
ICAjIFtQQVRDSDpjYWxpYi1zZXNzaW9uLXNraXBdIHNldCBieSBTQVZFIGluIGVpdGhlciBzdHVk
aW8KICAgICAgICAgc2VsZi5tYXN0ZXJfcGRmX3BhdGggPSBOb25lCiAgICAgICAgIHNlbGYuY2Fj
aGVkX3NpbG8yX2NvbnRlbnQgPSBOb25lCiAgICAgICAgIHNlbGYubWFzdGVyX2dyb3VwX2tleSA9
IE5vbmUKQEAgLTU5MCw3ICs0ODAsNyBAQCBjbGFzcyBMZWRnZXJHVUk6CiAgICAgICAgICAgICBz
ZWxmLmxvZ29fbGFiZWwgPSBjdGsuQ1RrTGFiZWwoc2VsZi5zaWRlYmFyLCB0ZXh0PSJbTG9nbyBN
aXNzaW5nXSIsIGZvbnQ9c2VsZi5oZWFkZXJfZm9udCkKICAgICAgICAgICAgIHNlbGYubG9nb19s
YWJlbC5ncmlkKHJvdz0wLCBjb2x1bW49MCwgcGFkeD0zMCwgcGFkeT0oNDAsIDIwKSkKIAotICAg
ICAgICBzZWxmLnN0YXR1c19sYWJlbCA9IGN0ay5DVGtMYWJlbChzZWxmLnNpZGViYXIsIHRleHQ9
IlNZU1RFTTogSURMRSIsIGZvbnQ9c2VsZi5oZWFkZXJfZm9udCwgdGV4dF9jb2xvcj0iIzFlODQ0
OSIpCisgICAgICAgIHNlbGYuc3RhdHVzX2xhYmVsID0gY3RrLkNUa0xhYmVsKHNlbGYuc2lkZWJh
ciwgdGV4dD0iU1lTVEVNOiBJRExFIiwgZm9udD1zZWxmLmhlYWRlcl9mb250LCB0ZXh0X2NvbG9y
PSIjMWU4NDQ5Iiwgd3JhcGxlbmd0aD0yNDApCiAgICAgICAgIHNlbGYuc3RhdHVzX2xhYmVsLmdy
aWQocm93PTEsIGNvbHVtbj0wLCBwYWR4PTIwLCBwYWR5PTEwKQogCiAgICAgICAgICMgTkVXIFVJ
OiBCbGFuayBNYXN0ZXIgUERGIFVwbG9hZGVyIFtjaXRlOiA5MiwgOTVdCkBAIC05MjcsMTYgKzgx
NywyNiBAQCBjbGFzcyBMZWRnZXJHVUk6CiAgICAgICAgICAgICAjIDYuIEluamVjdCBkZWZhdWx0
IFNjYW5uZXIgQ2FsaWJyYXRpb24gQ29vcmRpbmF0ZXMgKFgsIFksIFcsIEgpCiAgICAgICAgICAg
ICBjdXJzb3IuZXhlY3V0ZSgnJycKICAgICAgICAgICAgICAgICBJTlNFUlQgT1IgSUdOT1JFIElO
VE8gU2V0dGluZ3MgKFNldHRpbmdfS2V5LCBTZXR0aW5nX1ZhbHVlKQotICAgICAgICAgICAgICAg
IFZBTFVFUyAoJ2Nyb3BfdGl0bGUnLCAnMTkyLDIwNywxNTk4LDExOScpCisgICAgICAgICAgICAg
ICAgVkFMVUVTICgnY3JvcF90aXRsZScsICcyNTksMjE0LDc0MSwxNDgnKQogICAgICAgICAgICAg
JycnKQogICAgICAgICAgICAgY3Vyc29yLmV4ZWN1dGUoJycnCiAgICAgICAgICAgICAgICAgSU5T
RVJUIE9SIElHTk9SRSBJTlRPIFNldHRpbmdzIChTZXR0aW5nX0tleSwgU2V0dGluZ19WYWx1ZSkK
LSAgICAgICAgICAgICAgICBWQUxVRVMgKCdjcm9wX2lkJywgJzIzODAsMjM2LDEwNTAsMjUwJykK
KyAgICAgICAgICAgICAgICBWQUxVRVMgKCdjcm9wX2lkJywgJzE2NDcsMTY0LDY5MSwxODEnKQog
ICAgICAgICAgICAgJycnKQogICAgICAgICAgICAgY3Vyc29yLmV4ZWN1dGUoJycnCiAgICAgICAg
ICAgICAgICAgSU5TRVJUIE9SIElHTk9SRSBJTlRPIFNldHRpbmdzIChTZXR0aW5nX0tleSwgU2V0
dGluZ19WYWx1ZSkKICAgICAgICAgICAgICAgICBWQUxVRVMgKCdjcm9wX2Zvb3RlcicsICcxMjAw
LDMwOTMsMTMyLDE1OScpCiAgICAgICAgICAgICAnJycpCisgICAgICAgICAgICAjIFtQQVRDSDpj
YWxpYi1zZWVkXSBUaXRsZS9JRCBzZWVkcyA9IHRoZSBvcGVyYXRvcidzIG93biBMZXR0ZXIgY2Fs
aWJyYXRpb24KKyAgICAgICAgICAgICMgb2YgdGhlIExlZGdlciBmb3JtLiBUaGUgb2xkIElEIHNl
ZWQgKDIzODAsMjM2LDEwNTAsMjUwKSBiZWdhbiBhdCA5MyUgb2YgYQorICAgICAgICAgICAgIyBM
ZXR0ZXIgcGFnZSBhbmQgcmFuIG9mZiBpdHMgcmlnaHQgZWRnZTsgdGhlIG9sZCB0aXRsZSBzZWVk
IHJlYWNoZWQgNzAlIG9mCisgICAgICAgICAgICAjIHRoZSB3aWR0aCBhbmQgd291bGQgb3Zlcmxh
cCBhIGNvcnJlY3RseSBwbGFjZWQgSUQgYm94LiBBIERCIHdob3NlIHRocmVlCisgICAgICAgICAg
ICAjIHJvd3Mgc3RpbGwgZXF1YWwgdGhlIG9sZCBzZWVkcyB3YXMgbmV2ZXIgY2FsaWJyYXRlZCwg
c28gaXQgZ2V0cyB0aGUgbmV3CisgICAgICAgICAgICAjIHNlZWRzOyBhIERCIHdpdGggYW55IHNh
dmVkIGNhbGlicmF0aW9uIGlzIGxlZnQgYWxvbmUuCisgICAgICAgICAgICBjdXJzb3IuZXhlY3V0
ZSgiU0VMRUNUIFNldHRpbmdfS2V5LCBTZXR0aW5nX1ZhbHVlIEZST00gU2V0dGluZ3MgV0hFUkUg
U2V0dGluZ19LZXkgSU4gKCdjcm9wX3RpdGxlJywgJ2Nyb3BfaWQnLCAnY3JvcF9mb290ZXInKSIp
CisgICAgICAgICAgICBpZiBkaWN0KGN1cnNvci5mZXRjaGFsbCgpKSA9PSB7J2Nyb3BfdGl0bGUn
OiAnMTkyLDIwNywxNTk4LDExOScsICdjcm9wX2lkJzogJzIzODAsMjM2LDEwNTAsMjUwJywgJ2Ny
b3BfZm9vdGVyJzogJzEyMDAsMzA5MywxMzIsMTU5J306CisgICAgICAgICAgICAgICAgY3Vyc29y
LmV4ZWN1dGUoIlVQREFURSBTZXR0aW5ncyBTRVQgU2V0dGluZ19WYWx1ZSA9ICcyNTksMjE0LDc0
MSwxNDgnIFdIRVJFIFNldHRpbmdfS2V5ID0gJ2Nyb3BfdGl0bGUnIikKKyAgICAgICAgICAgICAg
ICBjdXJzb3IuZXhlY3V0ZSgiVVBEQVRFIFNldHRpbmdzIFNFVCBTZXR0aW5nX1ZhbHVlID0gJzE2
NDcsMTY0LDY5MSwxODEnIFdIRVJFIFNldHRpbmdfS2V5ID0gJ2Nyb3BfaWQnIikKICAgICAgICAg
ICAgIAogICAgICAgICAgICAgIyA3LiBbUEFUQ0g6ZXNzYXktbW9kZV0gSW5qZWN0IGRlZmF1bHQg
Z3JhZGluZyBtb2RlIChib3hlZCA9IHRoZQogICAgICAgICAgICAgIyBvcmlnaW5hbCBzdGl0Y2hl
ZC1ncmlkIHBpcGVsaW5lOyBlc3NheSA9IEZVTExfUEFHRSBwaXBlbGluZSkKQEAgLTE1MjMsNiAr
MTQyMywyMSBAQCBjbGFzcyBMZWRnZXJHVUk6CiAgICAgICAgICAgICBzZWxmLmxvZ19tZXNzYWdl
KGYiWyFdIERhdGFiYXNlIFJlYWQgRXJyb3IgKEdyYWRpbmcgTW9kZSk6IHtlfSIpCiAgICAgICAg
ICAgICByZXR1cm4gImJveGVkIgogCisgICAgZGVmIGdldF9ncmFkaW5nX2luc3RydWN0aW9ucyhz
ZWxmKToKKyAgICAgICAgIiIiW1BBVENIOm5vLWRlZmF1bHQtcHJvbXB0XSBXTzM6IHRoZSB0ZWFj
aGVyJ3MgZ3JhZGluZyBpbnN0cnVjdGlvbnMgZnJvbQorICAgICAgICB0aGUgU2V0dGluZ3MgREIu
IE5vdGhpbmcgc2hpcHMgYnVpbHQgaW46IG5vIHJvdyAoZnJlc2ggaW5zdGFsbCkgb3IgYQorICAg
ICAgICByZWFkIGVycm9yIHJldHVybnMgJycgYW5kIFJVTiByZWZ1c2VzIHRvIGdyYWRlLiIiIgor
ICAgICAgICB0cnk6CisgICAgICAgICAgICBjb25uID0gc3FsaXRlMy5jb25uZWN0KERCX1BBVEgp
CisgICAgICAgICAgICBjdXJzb3IgPSBjb25uLmN1cnNvcigpCisgICAgICAgICAgICBjdXJzb3Iu
ZXhlY3V0ZSgiU0VMRUNUIFNldHRpbmdfVmFsdWUgRlJPTSBTZXR0aW5ncyBXSEVSRSBTZXR0aW5n
X0tleSA9ICdncmFkaW5nX2luc3RydWN0aW9ucyciKQorICAgICAgICAgICAgcmVzdWx0ID0gY3Vy
c29yLmZldGNob25lKCkKKyAgICAgICAgICAgIGNvbm4uY2xvc2UoKQorICAgICAgICAgICAgcmV0
dXJuIHJlc3VsdFswXSBpZiAocmVzdWx0IGFuZCByZXN1bHRbMF0pIGVsc2UgIiIKKyAgICAgICAg
ZXhjZXB0IEV4Y2VwdGlvbiBhcyBlOgorICAgICAgICAgICAgc2VsZi5sb2dfbWVzc2FnZShmIlsh
XSBEYXRhYmFzZSBSZWFkIEVycm9yIChHcmFkaW5nIEluc3RydWN0aW9ucyk6IHtlfSIpCisgICAg
ICAgICAgICByZXR1cm4gIiIKKwogICAgIGRlZiBvbl9tb2RlX3NlbGVjdGVkKHNlbGYsIHNlbGVj
dGVkX3ZhbHVlKToKICAgICAgICAgIiIiW1BBVENIOmVzc2F5LW1vZGVdIFBlcnNpc3RzIHRoZSB0
ZWFjaGVyJ3MgbW9kZSBjaG9pY2UgaW1tZWRpYXRlbHkuIiIiCiAgICAgICAgIG1vZGUgPSAiZXNz
YXkiIGlmICJFU1NBWSIgaW4gc3RyKHNlbGVjdGVkX3ZhbHVlKS51cHBlcigpIGVsc2UgImJveGVk
IgpAQCAtMTY1MCw3ICsxNTY1LDcgQEAgY2xhc3MgTGVkZ2VyR1VJOgogICAgICAgICBleGNlcHQg
RXhjZXB0aW9uIGFzIGU6CiAgICAgICAgICAgICBzZWxmLmxvZ19tZXNzYWdlKGYiWyFdIERCIEVy
cm9yIHJlYWRpbmcge2tleX06IHtlfSIpCiAgICAgICAgICMgRmFpbHNhZmUgZGVmYXVsdHMgbWly
cm9yaW5nIHRoZSBpbml0aWFsIERCIGNyZWF0aW9uCi0gICAgICAgIGRlZmF1bHRzID0geydjcm9w
X3RpdGxlJzogKDE5MiwgMjA3LCAxNTk4LCAxMTkpLCAnY3JvcF9pZCc6ICgyMzgwLCAyMzYsIDEw
NTAsIDI1MCksICdjcm9wX2Zvb3Rlcic6ICgxMjAwLCAzMDkzLCAxMzIsIDE1OSl9CisgICAgICAg
IGRlZmF1bHRzID0geydjcm9wX3RpdGxlJzogKDI1OSwgMjE0LCA3NDEsIDE0OCksICdjcm9wX2lk
JzogKDE2NDcsIDE2NCwgNjkxLCAxODEpLCAnY3JvcF9mb290ZXInOiAoMTIwMCwgMzA5MywgMTMy
LCAxNTkpfQogICAgICAgICByZXR1cm4gZGVmYXVsdHMuZ2V0KGtleSwgKDAsIDAsIDEwMCwgMTAw
KSkKIAogICAgIGRlZiBzZXRfdXNlcl9tb2RlbChzZWxmLCBtb2RlbF9uYW1lKToKQEAgLTIwMTAs
NiArMTkyNSw3IEBAIGNsYXNzIExlZGdlckdVSToKICAgICAgICAgICAgIAogICAgICAgICAgICAg
c2VsZi5fbWFrZV90ZXh0X2NvcHlhYmxlKGluZm9fd2luKQogICAgICAgICAgICAgaW5mb193aW4u
Z3JhYl9zZXQoKSAjIExvY2sgZm9jdXMgdG8gdGhpcyBwb3B1cAorICAgICAgICAgICAgaWYgaXNf
aW50ZXJjZXB0OiBzZWxmLnNodXRfZG93bigpICAgIyBbUEFUQ0g6Y2FsaWItY2xvc2UtcmVzZXRd
IEQxYjogUlVOIGNhbm5vdCBwcm9jZWVkCiAgICAgICAgICAgICByZXR1cm4KIAogICAgICAgICAj
IDIuIFJlbmRlciB0aGUgbWFzc2l2ZSBuYXRpdmUgaW1hZ2UKQEAgLTIwMjIsNiArMTkzOCw3IEBA
IGNsYXNzIExlZGdlckdVSToKICAgICAgICAgICAgIGRvYy5jbG9zZSgpCiAgICAgICAgIGV4Y2Vw
dCBFeGNlcHRpb24gYXMgZToKICAgICAgICAgICAgIHNlbGYubG9nX21lc3NhZ2UoZiJbIV0gRXJy
b3IgcmVuZGVyaW5nIGNhbGlicmF0aW9uIHByZXZpZXc6IHtlfSIpCisgICAgICAgICAgICBpZiBp
c19pbnRlcmNlcHQ6IHNlbGYuc2h1dF9kb3duKCkgICAjIFtQQVRDSDpjYWxpYi1jbG9zZS1yZXNl
dF0gRDFiOiBSVU4gY2Fubm90IHByb2NlZWQKICAgICAgICAgICAgIHJldHVybgogCiAgICAgICAg
ICMgMy4gQ2FsY3VsYXRlIHNjYWxpbmcgbWF0aCB0byBmaXQgVUkKQEAgLTIwMjksNyArMTk0Niwx
MCBAQCBjbGFzcyBMZWRnZXJHVUk6CiAgICAgICAgIHJhd19pbWcgPSBJbWFnZS5vcGVuKHRlbXBf
aW1nX3BhdGgpCiAgICAgICAgIG5hdGl2ZV93LCBuYXRpdmVfaCA9IHJhd19pbWcuc2l6ZQogICAg
ICAgICAKLSAgICAgICAgdGFyZ2V0X3VpX2hlaWdodCA9IDgwMAorICAgICAgICAjIFtQQVRDSDpj
YWxpYi1maXRdIGZpdCB0aGUgc3R1ZGlvIHRvIHRoZSBzY3JlZW4gKHRoZSBkYXNoYm9hcmQncyBj
bGFtcCk6CisgICAgICAgICMgYXQgYSBmaXhlZCA4MDAgcHggdGhlIHBhZ2UtbnVtYmVyIGJveCBh
bmQgU0FWRSBzYXQgYmVsb3cgdGhlIGJvdHRvbSBvZgorICAgICAgICAjIGEgNzY4LXB4IGRpc3Bs
YXkuIFN0b3JlZCBjb29yZGluYXRlcyBhcmUgbmF0aXZlIHBpeGVscywgc28gdW5hZmZlY3RlZC4K
KyAgICAgICAgdGFyZ2V0X3VpX2hlaWdodCA9IG1pbig4MDAsIHNlbGYucm9vdC53aW5mb19zY3Jl
ZW5oZWlnaHQoKSAtIFdJWkFSRF9TQ1JFRU5fQUxMT1dBTkNFIC0gMTIwKQogICAgICAgICBzY2Fs
ZV9mYWN0b3IgPSB0YXJnZXRfdWlfaGVpZ2h0IC8gbmF0aXZlX2gKICAgICAgICAgdWlfdyA9IGlu
dChuYXRpdmVfdyAqIHNjYWxlX2ZhY3RvcikKICAgICAgICAgCkBAIC0yMDQxLDYgKzE5NjEsMTcg
QEAgY2xhc3MgTGVkZ2VyR1VJOgogICAgICAgICBjYWxpYl93aW4uZ2VvbWV0cnkoZiJ7dWlfdyAr
IDQwfXh7dGFyZ2V0X3VpX2hlaWdodCArIDEyMH0iKQogICAgICAgICBjYWxpYl93aW4udHJhbnNp
ZW50KHNlbGYucm9vdCkKICAgICAgICAgCisgICAgICAgICMgW1BBVENIOmNhbGliLWNsb3NlLXJl
c2V0XSBXTzIgRDE6IHdoZW4gUlVOIG9wZW5lZCB0aGlzIHN0dWRpbywgY2xvc2luZworICAgICAg
ICAjIGl0IHdpdGggdGhlIHdpbmRvdydzIFggbGVmdCBpc19ydW5uaW5nPVRydWUgYW5kIFJVTiBk
aXNhYmxlZCwgc28gdGhlCisgICAgICAgICMgYmF0Y2ggY291bGQgbmV2ZXIgc3RhcnQgKENBTElC
UkFURSBTQ0FOTkVSIG9wZW5zIHRoZSBzdHVkaW8gc2F2ZS1vbmx5KS4KKyAgICAgICAgIyBUaGUg
WCBub3cgY2FuY2VscyB0aGUgcnVuLCBleGFjdGx5IGFzIFNIVVQgRE9XTiBkb2VzLgorICAgICAg
ICBpZiBpc19pbnRlcmNlcHQ6CisgICAgICAgICAgICBkZWYgY2FuY2VsX2ludGVyY2VwdCgpOgor
ICAgICAgICAgICAgICAgIGNhbGliX3dpbi5kZXN0cm95KCkKKyAgICAgICAgICAgICAgICBzZWxm
LmxvZ19tZXNzYWdlKCJDYWxpYnJhdGlvbiBjbG9zZWQgd2l0aG91dCBzYXZpbmcgLS0gcnVuIGNh
bmNlbGxlZC4iKQorICAgICAgICAgICAgICAgIHNlbGYuc2h1dF9kb3duKCkKKyAgICAgICAgICAg
IGNhbGliX3dpbi5wcm90b2NvbCgiV01fREVMRVRFX1dJTkRPVyIsIGNhbmNlbF9pbnRlcmNlcHQp
CisKICAgICAgICAgaGVhZGVyX2xhYmVsID0gY3RrLkNUa0xhYmVsKGNhbGliX3dpbiwgdGV4dD0i
QURKVVNUIERPQ1VNRU5UIENST1AgWk9ORVMiLCBmb250PXNlbGYuaGVhZGVyX2ZvbnQpCiAgICAg
ICAgIGhlYWRlcl9sYWJlbC5wYWNrKHBhZHk9KDEwLCA1KSkKICAgICAgICAgCkBAIC0yMTMzLDEz
ICsyMDY0LDE2IEBAIGNsYXNzIExlZGdlckdVSToKICAgICAgICAgICAgICAgICByZXR1cm4gKGlu
dCh4MSksIGludCh5MSksIGludCh4MiAtIHgxKSwgaW50KHkyIC0geTEpKQogCiAgICAgICAgICMg
Ni4gRmV0Y2ggbmF0aXZlIGNvb3JkcyBhbmQgZ2VuZXJhdGUgdGhlIGJveGVzCisgICAgICAgICMg
W1BBVENIOmNhbGliLXJvdW5kXSByb3VuZCgpLCBub3QgaW50KCksIGhlcmUgYW5kIGluIHNhdmVf
YW5kX3Byb2NlZWQ6CisgICAgICAgICMgdHJ1bmNhdGluZyBib3RoIHdheXMgY29zdCB+NCBuYXRp
dmUgcHggcGVyIG9wZW4rc2F2ZSwgc28gdW50b3VjaGVkCisgICAgICAgICMgYm94ZXMgY3JlcHQg
dXAtbGVmdCBhbmQgc2hyYW5rIG9uIGV2ZXJ5IFJVTi4KICAgICAgICAgdF9jb29yZHMgPSBzZWxm
LmdldF9jcm9wX2Nvb3JkaW5hdGVzKCdjcm9wX3RpdGxlJykKICAgICAgICAgaWRfY29vcmRzID0g
c2VsZi5nZXRfY3JvcF9jb29yZGluYXRlcygnY3JvcF9pZCcpCiAgICAgICAgIGZfY29vcmRzID0g
c2VsZi5nZXRfY3JvcF9jb29yZGluYXRlcygnY3JvcF9mb290ZXInKQogICAgICAgICAKLSAgICAg
ICAgYm94X3RpdGxlID0gQ2FsaWJyYXRpb25Cb3goY2FudmFzLCAiVGl0bGUiLCBpbnQodF9jb29y
ZHNbMF0qc2NhbGVfZmFjdG9yKSwgaW50KHRfY29vcmRzWzFdKnNjYWxlX2ZhY3RvciksIGludCh0
X2Nvb3Jkc1syXSpzY2FsZV9mYWN0b3IpLCBpbnQodF9jb29yZHNbM10qc2NhbGVfZmFjdG9yKSwg
IiMzNDk4ZGIiKSAjIEJsdWUKLSAgICAgICAgYm94X2lkID0gQ2FsaWJyYXRpb25Cb3goY2FudmFz
LCAiSUQiLCBpbnQoaWRfY29vcmRzWzBdKnNjYWxlX2ZhY3RvciksIGludChpZF9jb29yZHNbMV0q
c2NhbGVfZmFjdG9yKSwgaW50KGlkX2Nvb3Jkc1syXSpzY2FsZV9mYWN0b3IpLCBpbnQoaWRfY29v
cmRzWzNdKnNjYWxlX2ZhY3RvciksICIjMmVjYzcxIikgIyBHcmVlbgotICAgICAgICBib3hfZm9v
dGVyID0gQ2FsaWJyYXRpb25Cb3goY2FudmFzLCAiUGFnZSBOdW1iZXIiLCBpbnQoZl9jb29yZHNb
MF0qc2NhbGVfZmFjdG9yKSwgaW50KGZfY29vcmRzWzFdKnNjYWxlX2ZhY3RvciksIGludChmX2Nv
b3Jkc1syXSpzY2FsZV9mYWN0b3IpLCBpbnQoZl9jb29yZHNbM10qc2NhbGVfZmFjdG9yKSwgIiNl
NzRjM2MiKSAjIFJlZAorICAgICAgICBib3hfdGl0bGUgPSBDYWxpYnJhdGlvbkJveChjYW52YXMs
ICJUaXRsZSIsIHJvdW5kKHRfY29vcmRzWzBdKnNjYWxlX2ZhY3RvciksIHJvdW5kKHRfY29vcmRz
WzFdKnNjYWxlX2ZhY3RvciksIHJvdW5kKHRfY29vcmRzWzJdKnNjYWxlX2ZhY3RvciksIHJvdW5k
KHRfY29vcmRzWzNdKnNjYWxlX2ZhY3RvciksICIjMzQ5OGRiIikgIyBCbHVlCisgICAgICAgIGJv
eF9pZCA9IENhbGlicmF0aW9uQm94KGNhbnZhcywgIklEIiwgcm91bmQoaWRfY29vcmRzWzBdKnNj
YWxlX2ZhY3RvciksIHJvdW5kKGlkX2Nvb3Jkc1sxXSpzY2FsZV9mYWN0b3IpLCByb3VuZChpZF9j
b29yZHNbMl0qc2NhbGVfZmFjdG9yKSwgcm91bmQoaWRfY29vcmRzWzNdKnNjYWxlX2ZhY3Rvciks
ICIjMmVjYzcxIikgIyBHcmVlbgorICAgICAgICBib3hfZm9vdGVyID0gQ2FsaWJyYXRpb25Cb3go
Y2FudmFzLCAiUGFnZSBOdW1iZXIiLCByb3VuZChmX2Nvb3Jkc1swXSpzY2FsZV9mYWN0b3IpLCBy
b3VuZChmX2Nvb3Jkc1sxXSpzY2FsZV9mYWN0b3IpLCByb3VuZChmX2Nvb3Jkc1syXSpzY2FsZV9m
YWN0b3IpLCByb3VuZChmX2Nvb3Jkc1szXSpzY2FsZV9mYWN0b3IpLCAiI2U3NGMzYyIpICMgUmVk
CiAgICAgICAgIAogICAgICAgICAjIDcuIFNhdmUgUHJvdG9jb2wKICAgICAgICAgZGVmIHNhdmVf
YW5kX3Byb2NlZWQoKToKQEAgLTIxNDgsOSArMjA4Miw5IEBAIGNsYXNzIExlZGdlckdVSToKICAg
ICAgICAgICAgICAgICBpZF91aSA9IGJveF9pZC5nZXRfY29vcmRzKCkKICAgICAgICAgICAgICAg
ICBmX3VpID0gYm94X2Zvb3Rlci5nZXRfY29vcmRzKCkKICAgICAgICAgICAgICAgICAKLSAgICAg
ICAgICAgICAgICB0X25hdCA9IGYie2ludCh0X3VpWzBdL3NjYWxlX2ZhY3Rvcil9LHtpbnQodF91
aVsxXS9zY2FsZV9mYWN0b3IpfSx7aW50KHRfdWlbMl0vc2NhbGVfZmFjdG9yKX0se2ludCh0X3Vp
WzNdL3NjYWxlX2ZhY3Rvcil9IgotICAgICAgICAgICAgICAgIGlkX25hdCA9IGYie2ludChpZF91
aVswXS9zY2FsZV9mYWN0b3IpfSx7aW50KGlkX3VpWzFdL3NjYWxlX2ZhY3Rvcil9LHtpbnQoaWRf
dWlbMl0vc2NhbGVfZmFjdG9yKX0se2ludChpZF91aVszXS9zY2FsZV9mYWN0b3IpfSIKLSAgICAg
ICAgICAgICAgICBmX25hdCA9IGYie2ludChmX3VpWzBdL3NjYWxlX2ZhY3Rvcil9LHtpbnQoZl91
aVsxXS9zY2FsZV9mYWN0b3IpfSx7aW50KGZfdWlbMl0vc2NhbGVfZmFjdG9yKX0se2ludChmX3Vp
WzNdL3NjYWxlX2ZhY3Rvcil9IgorICAgICAgICAgICAgICAgIHRfbmF0ID0gZiJ7cm91bmQodF91
aVswXS9zY2FsZV9mYWN0b3IpfSx7cm91bmQodF91aVsxXS9zY2FsZV9mYWN0b3IpfSx7cm91bmQo
dF91aVsyXS9zY2FsZV9mYWN0b3IpfSx7cm91bmQodF91aVszXS9zY2FsZV9mYWN0b3IpfSIKKyAg
ICAgICAgICAgICAgICBpZF9uYXQgPSBmIntyb3VuZChpZF91aVswXS9zY2FsZV9mYWN0b3IpfSx7
cm91bmQoaWRfdWlbMV0vc2NhbGVfZmFjdG9yKX0se3JvdW5kKGlkX3VpWzJdL3NjYWxlX2ZhY3Rv
cil9LHtyb3VuZChpZF91aVszXS9zY2FsZV9mYWN0b3IpfSIKKyAgICAgICAgICAgICAgICBmX25h
dCA9IGYie3JvdW5kKGZfdWlbMF0vc2NhbGVfZmFjdG9yKX0se3JvdW5kKGZfdWlbMV0vc2NhbGVf
ZmFjdG9yKX0se3JvdW5kKGZfdWlbMl0vc2NhbGVfZmFjdG9yKX0se3JvdW5kKGZfdWlbM10vc2Nh
bGVfZmFjdG9yKX0iCiAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgdHJ5OgogICAg
ICAgICAgICAgICAgICAgICBjb25uID0gc3FsaXRlMy5jb25uZWN0KERCX1BBVEgpCkBAIC0yMTYx
LDYgKzIwOTUsNyBAQCBjbGFzcyBMZWRnZXJHVUk6CiAgICAgICAgICAgICAgICAgICAgIGNvbm4u
Y29tbWl0KCkKICAgICAgICAgICAgICAgICAgICAgY29ubi5jbG9zZSgpCiAgICAgICAgICAgICAg
ICAgICAgIHNlbGYubG9nX21lc3NhZ2UoIi0+IENhbGlicmF0aW9uIFpvbmVzIExvY2tlZCAmIFNh
dmVkLiIpCisgICAgICAgICAgICAgICAgICAgIHNlbGYuY2FsaWJyYXRlZF90aGlzX3Nlc3Npb24g
PSBUcnVlICAgIyBbUEFUQ0g6Y2FsaWItc2Vzc2lvbi1za2lwXQogICAgICAgICAgICAgICAgIGV4
Y2VwdCBFeGNlcHRpb24gYXMgZToKICAgICAgICAgICAgICAgICAgICAgc2VsZi5sb2dfbWVzc2Fn
ZShmIlshXSBEQiBFcnJvciBzYXZpbmcgY29vcmRpbmF0ZXM6IHtlfSIpCiAgICAgICAgICAgICAg
ICAgICAgIApAQCAtMjE3Miw3ICsyMTA3LDcgQEAgY2xhc3MgTGVkZ2VyR1VJOgogICAgICAgICAg
ICAgICAgICAgICAjIEFSQ0hJVEVDVFVSQUwgRklYOiBQdXNoIHRoZSBibG9ja2luZyBBUEkgZXh0
cmFjdGlvbiBpbnRvIGEgYmFja2dyb3VuZCB0aHJlYWQKICAgICAgICAgICAgICAgICAgICAgdGhy
ZWFkaW5nLlRocmVhZCh0YXJnZXQ9c2VsZi5ydW5fYmF0Y2hfZXh0cmFjdGlvbiwgYXJncz0odGFy
Z2V0X3BkZiwpLCBkYWVtb249VHJ1ZSkuc3RhcnQoKQogICAgICAgICAgICAgICAgIAotICAgICAg
ICBzYXZlX2J0biA9IGN0ay5DVGtCdXR0b24oY2FsaWJfd2luLCB0ZXh0PSJTQVZFICYgUFJPQ0VF
RCIsIGZvbnQ9c2VsZi5oZWFkZXJfZm9udCwgaGVpZ2h0PTUwLCBmZ19jb2xvcj0iIzFlODQ0OSIs
IGhvdmVyX2NvbG9yPSIjMjdhZTYwIiwgY29tbWFuZD1zYXZlX2FuZF9wcm9jZWVkKQorICAgICAg
ICBzYXZlX2J0biA9IGN0ay5DVGtCdXR0b24oY2FsaWJfd2luLCB0ZXh0PSJTQVZFICYgUFJPQ0VF
RCIgaWYgaXNfaW50ZXJjZXB0IGVsc2UgIlNBVkUiLCBmb250PXNlbGYuaGVhZGVyX2ZvbnQsIGhl
aWdodD01MCwgZmdfY29sb3I9IiMxZTg0NDkiLCBob3Zlcl9jb2xvcj0iIzI3YWU2MCIsIGNvbW1h
bmQ9c2F2ZV9hbmRfcHJvY2VlZCkKICAgICAgICAgc2F2ZV9idG4ucGFjayhwYWR5PSgxMCwgMjAp
KQogICAgICAgICAKICAgICAgICAgc2VsZi5fbWFrZV90ZXh0X2NvcHlhYmxlKGNhbGliX3dpbikK
QEAgLTI1NjMsOSArMjQ5OCw5IEBAIGNsYXNzIExlZGdlckdVSToKICAgICAgICAgc2VsZi5fbWFr
ZV90ZXh0X2NvcHlhYmxlKGFzc25fd2luKQogCiAgICAgZGVmIG9wZW5fcHJvbXB0X2VkaXRvcihz
ZWxmKToKLSAgICAgICAgIiIiTGF1bmNoZXMgdGhlIFNpbG8gMiBQcm9tcHQgRWRpdG9yIGludGVy
ZmFjZS4iIiIKKyAgICAgICAgIiIiTGF1bmNoZXMgdGhlIEdSQURJTkcgSU5TVFJVQ1RJT05TIGVk
aXRvciAodGhlIHRlYWNoZXIncyBwcm9tcHQpLiIiIgogICAgICAgICBlZGl0b3Jfd2luID0gY3Rr
LkNUa1RvcGxldmVsKHNlbGYucm9vdCkKLSAgICAgICAgZWRpdG9yX3dpbi50aXRsZSgiQUkgUFJP
TVBUIEVESVRPUiIpCisgICAgICAgIGVkaXRvcl93aW4udGl0bGUoIkdSQURJTkcgSU5TVFJVQ1RJ
T05TIikKICAgICAgICAgZWRpdG9yX3dpbi5nZW9tZXRyeSgiOTAweDcwMCIpCiAgICAgICAgIGVk
aXRvcl93aW4udHJhbnNpZW50KHNlbGYucm9vdCkgCiAgICAgICAgIApAQCAtMjU3NCw3ICsyNTA5
LDcgQEAgY2xhc3MgTGVkZ2VyR1VJOgogICAgICAgICBlZGl0b3Jfd2luLmdyaWRfY29sdW1uY29u
ZmlndXJlKDAsIHdlaWdodD0xKQogCiAgICAgICAgICMgSGVhZGVyCi0gICAgICAgIGhlYWRlcl9s
YWJlbCA9IGN0ay5DVGtMYWJlbChlZGl0b3Jfd2luLCB0ZXh0PSJTSUxPIDIgU1lTVEVNIElOU1RS
VUNUSU9OUyIsIGZvbnQ9c2VsZi5oZWFkZXJfZm9udCkKKyAgICAgICAgaGVhZGVyX2xhYmVsID0g
Y3RrLkNUa0xhYmVsKGVkaXRvcl93aW4sIHRleHQ9IkdSQURJTkcgSU5TVFJVQ1RJT05TIiwgZm9u
dD1zZWxmLmhlYWRlcl9mb250KQogICAgICAgICBoZWFkZXJfbGFiZWwuZ3JpZChyb3c9MCwgY29s
dW1uPTAsIHBhZHk9KDIwLCAxMCkpCiAKICAgICAgICAgIyBSb3cgMSBpcyBpbnRlbnRpb25hbGx5
IGVtcHR5IChbUEFUQ0g6bm8tdGVtcGVyYXR1cmVdIHRoZSBzYW1wbGluZwpAQCAtMjYxMiwxMyAr
MjU0NywxMCBAQCBjbGFzcyBMZWRnZXJHVUk6CiAgICAgICAgIAogICAgICAgICBzZWxmLnByb21w
dF90ZXh0Ym94LmJpbmQoIjxCdXR0b24tMz4iLCBsYW1iZGEgZXZlbnQ6IGVkaXRvcl9tZW51LnRr
X3BvcHVwKGV2ZW50Lnhfcm9vdCwgZXZlbnQueV9yb290KSkKIAotICAgICAgICAjIExvYWQgY3Vy
cmVudCB0ZXh0IGZyb20gdGhlIGV4dGVybmFsIGZpbGUKLSAgICAgICAgdHJ5OgotICAgICAgICAg
ICAgd2l0aCBvcGVuKFBST01QVF9GSUxFLCAiciIsIGVuY29kaW5nPSJ1dGYtOCIpIGFzIGY6Ci0g
ICAgICAgICAgICAgICAgY3VycmVudF9wcm9tcHQgPSBmLnJlYWQoKQotICAgICAgICAgICAgICAg
IHNlbGYucHJvbXB0X3RleHRib3guaW5zZXJ0KCIwLjAiLCBjdXJyZW50X3Byb21wdCkKLSAgICAg
ICAgZXhjZXB0IEV4Y2VwdGlvbiBhcyBlOgotICAgICAgICAgICAgc2VsZi5wcm9tcHRfdGV4dGJv
eC5pbnNlcnQoIjAuMCIsIGYiRXJyb3IgbG9hZGluZyBwcm9tcHQ6IHtlfSIpCisgICAgICAgICMg
W1BBVENIOm5vLWRlZmF1bHQtcHJvbXB0XSBMb2FkIHRoZSBzYXZlZCBpbnN0cnVjdGlvbnMgZnJv
bSB0aGUgREIuCisgICAgICAgICMgRW1wdHkgb24gYSBmcmVzaCBpbnN0YWxsOyBhIHJlYWQgZXJy
b3IgbGVhdmVzIHRoZSBib3ggZW1wdHkgKGl0IGlzCisgICAgICAgICMgbG9nZ2VkKSBzbyBlcnJv
ciB0ZXh0IGNhbiBuZXZlciBiZSBzYXZlZCBiYWNrIGFzIGEgcHJvbXB0LgorICAgICAgICBzZWxm
LnByb21wdF90ZXh0Ym94Lmluc2VydCgiMC4wIiwgc2VsZi5nZXRfZ3JhZGluZ19pbnN0cnVjdGlv
bnMoKSkKIAogICAgICAgICAjIFNhdmUgQnV0dG9uCiAgICAgICAgIHNhdmVfYnRuID0gY3RrLkNU
a0J1dHRvbigKQEAgLTI2MzQsMTcgKzI1NjYsMjUgQEAgY2xhc3MgTGVkZ2VyR1VJOgogICAgICAg
ICBzZWxmLl9tYWtlX3RleHRfY29weWFibGUoZWRpdG9yX3dpbikKIAogICAgIGRlZiBzYXZlX3By
b21wdChzZWxmLCB3aW5kb3cpOgotICAgICAgICAiIiJXcml0ZXMgdGhlIHRleHRib3ggY29udGVu
dHMgYmFjayB0byB0aGUgZXh0ZXJuYWwgZmlsZS4iIiIKKyAgICAgICAgIiIiV3JpdGVzIHRoZSB0
ZXh0Ym94IGNvbnRlbnRzIHRvIHRoZSBTZXR0aW5ncyBEQiAoJ2dyYWRpbmdfaW5zdHJ1Y3Rpb25z
JykuIiIiCiAgICAgICAgIG5ld19wcm9tcHRfdGV4dCA9IHNlbGYucHJvbXB0X3RleHRib3guZ2V0
KCIwLjAiLCAiZW5kIikuc3RyaXAoKQogCiAgICAgICAgIHRyeToKLSAgICAgICAgICAgIHdpdGgg
b3BlbihQUk9NUFRfRklMRSwgInciLCBlbmNvZGluZz0idXRmLTgiKSBhcyBmOgotICAgICAgICAg
ICAgICAgIGYud3JpdGUobmV3X3Byb21wdF90ZXh0KQorICAgICAgICAgICAgY29ubiA9IHNxbGl0
ZTMuY29ubmVjdChEQl9QQVRIKQorICAgICAgICAgICAgY3Vyc29yID0gY29ubi5jdXJzb3IoKQor
ICAgICAgICAgICAgY3Vyc29yLmV4ZWN1dGUoJycnCisgICAgICAgICAgICAgICAgSU5TRVJUIE9S
IFJFUExBQ0UgSU5UTyBTZXR0aW5ncyAoU2V0dGluZ19LZXksIFNldHRpbmdfVmFsdWUpCisgICAg
ICAgICAgICAgICAgVkFMVUVTICgnZ3JhZGluZ19pbnN0cnVjdGlvbnMnLCA/KQorICAgICAgICAg
ICAgJycnLCAobmV3X3Byb21wdF90ZXh0LCkpCisgICAgICAgICAgICBjb25uLmNvbW1pdCgpCisg
ICAgICAgICAgICBjb25uLmNsb3NlKCkKIAotICAgICAgICAgICAgc2VsZi5sb2dfbWVzc2FnZSgi
LT4gU2lsbyAyIFByb21wdCBzYXZlZC4iKQorICAgICAgICAgICAgc2VsZi5sb2dfbWVzc2FnZShm
Ii0+IEdyYWRpbmcgaW5zdHJ1Y3Rpb25zIHNhdmVkICh7bGVuKG5ld19wcm9tcHRfdGV4dCl9IGNo
YXJhY3RlcnMpLiIpCisgICAgICAgICAgICBpZiBub3Qgc2VsZi5pc19ydW5uaW5nOgorICAgICAg
ICAgICAgICAgIHNlbGYudXBkYXRlX2luZGljYXRvcigiaWRsZSIpICAgIyBjbGVhcnMgYSBOTyBH
UkFESU5HIElOU1RSVUNUSU9OUyBiYW5uZXIKICAgICAgICAgICAgIHdpbmRvdy5kZXN0cm95KCkK
ICAgICAgICAgZXhjZXB0IEV4Y2VwdGlvbiBhcyBlOgotICAgICAgICAgICAgc2VsZi5sb2dfbWVz
c2FnZShmIlshXSBFcnJvciBzYXZpbmcgcHJvbXB0IHNldHRpbmdzOiB7ZX0iKQorICAgICAgICAg
ICAgc2VsZi5sb2dfbWVzc2FnZShmIlshXSBEYXRhYmFzZSBXcml0ZSBFcnJvciAoR3JhZGluZyBJ
bnN0cnVjdGlvbnMpOiB7ZX0iKQogCiAgICAgZGVmIG9wZW5fZ3JhZGVib29rKHNlbGYpOgogICAg
ICAgICAiIiJMYXVuY2hlcyB0aGUgVmlzdWFsIFNJUyBpbnRlcmZhY2UgYW5kIHBvcHVsYXRlcyBp
dCBmcm9tIFNRTGl0ZS4iIiIKQEAgLTMxMzEsNiArMzA3MSwxNCBAQCBjbGFzcyBMZWRnZXJHVUk6
CiAgICAgICAgICAgICBzZWxmLnN0YXR1c19sYWJlbC5jb25maWd1cmUodGV4dD0iU1lTVEVNOiBJ
RExFIiwgdGV4dF9jb2xvcj0iIzFlODQ0OSIpCiAKICAgICBkZWYgc3RhcnRfbGVkZ2VyKHNlbGYp
OgorICAgICAgICAjIFtQQVRDSDpuby1kZWZhdWx0LXByb21wdF0gRW1wdHkgKG9yIHdoaXRlc3Bh
Y2Utb25seSkgaW5zdHJ1Y3Rpb25zOgorICAgICAgICAjIHJlZnVzZSBiZWZvcmUgYW55dGhpbmcg
c3RhcnRzIC0tIG5vIGNhbGlicmF0aW9uIHdpbmRvdywgbm8gYmF0Y2gsCisgICAgICAgICMgbm90
aGluZyBzZW50IHRvIHRoZSBkYWVtb24uIFRoZSBzeXN0ZW0gbmV2ZXIgbGVhdmVzIGlkbGUuCisg
ICAgICAgIGlmIG5vdCBzZWxmLmlzX3J1bm5pbmcgYW5kIG5vdCBzZWxmLmdldF9ncmFkaW5nX2lu
c3RydWN0aW9ucygpLnN0cmlwKCk6CisgICAgICAgICAgICBzZWxmLnN0YXR1c19sYWJlbC5jb25m
aWd1cmUodGV4dD1OT19JTlNUUlVDVElPTlNfQkFOTkVSLCB0ZXh0X2NvbG9yPSIjYzAzOTJiIikK
KyAgICAgICAgICAgIHNlbGYubG9nX21lc3NhZ2UoZiJbIV0ge05PX0lOU1RSVUNUSU9OU19MT0d9
IikKKyAgICAgICAgICAgIHJldHVybgorCiAgICAgICAgIG1heF9zY29yZV9zdHIgPSBzZWxmLm1h
eF9zY29yZV9lbnRyeS5nZXQoKS5zdHJpcCgpCiAgICAgICAgIGlmIG5vdCBtYXhfc2NvcmVfc3Ry
LmlzZGlnaXQoKSBvciBpbnQobWF4X3Njb3JlX3N0cikgPD0gMDoKICAgICAgICAgICAgIHNlbGYu
bG9nX21lc3NhZ2UoIlshXSBFcnJvcjogRW50ZXIgYSB2YWxpZCBNYXggU2NvcmUuIikKQEAgLTMx
NzQsNyArMzEyMiwxNiBAQCBjbGFzcyBMZWRnZXJHVUk6CiAgICAgICAgICAgICBzZWxmLnVwZGF0
ZV9pbmRpY2F0b3IoInJ1bm5pbmciKQogICAgICAgICAgICAgCiAgICAgICAgICAgICAjIElOVEVS
Q0VQVDogT3BlbiBDYWxpYnJhdGlvbiBTdHVkaW8gZm9yIHZpc3VhbCBjaGVjayBpbnN0ZWFkIG9m
IGluc3RhbnQgcnVuCi0gICAgICAgICAgICBzZWxmLm9wZW5fY2FsaWJyYXRpb25fdG9vbChpc19p
bnRlcmNlcHQ9VHJ1ZSkKKyAgICAgICAgICAgICMgW1BBVENIOmNhbGliLXNlc3Npb24tc2tpcF0g
Li4udW5sZXNzIHpvbmVzIHdlcmUgU0FWRUQgaW4gdGhpcyBncmFkZXIKKyAgICAgICAgICAgICMg
c2Vzc2lvbiAoZWl0aGVyIHN0dWRpbyk7IGEgcmVzdGFydCBjbGVhcnMgdGhhdC4gTm8gc2NhbiAt
PiBzdHVkaW8sCisgICAgICAgICAgICAjIHdoaWNoIHNob3dzIFNDQU4gUkVRVUlSRUQgYW5kIHJl
c2V0cyB0aGUgcnVuLgorICAgICAgICAgICAgc2Nhbl9maWxlcyA9IFtmIGZvciBmIGluIG9zLmxp
c3RkaXIoVU5FVkFMVUFURURfRElSKSBpZiBmLmxvd2VyKCkuZW5kc3dpdGgoJy5wZGYnKV0KKyAg
ICAgICAgICAgIGlmIHNlbGYuY2FsaWJyYXRlZF90aGlzX3Nlc3Npb24gYW5kIHNjYW5fZmlsZXM6
CisgICAgICAgICAgICAgICAgdGFyZ2V0X3BkZiA9IG9zLnBhdGguam9pbihVTkVWQUxVQVRFRF9E
SVIsIHNjYW5fZmlsZXNbMF0pCisgICAgICAgICAgICAgICAgc2VsZi5sb2dfbWVzc2FnZSgiQ2Fs
aWJyYXRpb24gYWxyZWFkeSBzYXZlZCB0aGlzIHNlc3Npb24uIFN0YXJ0aW5nIExlZGdlciBlbmdp
bmUuLi4iKQorICAgICAgICAgICAgICAgIHRocmVhZGluZy5UaHJlYWQodGFyZ2V0PXNlbGYucnVu
X2JhdGNoX2V4dHJhY3Rpb24sIGFyZ3M9KHRhcmdldF9wZGYsKSwgZGFlbW9uPVRydWUpLnN0YXJ0
KCkKKyAgICAgICAgICAgIGVsc2U6CisgICAgICAgICAgICAgICAgc2VsZi5vcGVuX2NhbGlicmF0
aW9uX3Rvb2woaXNfaW50ZXJjZXB0PVRydWUpCiAKICAgICBkZWYgc2h1dF9kb3duKHNlbGYpOgog
ICAgICAgICBzZWxmLmlzX3J1bm5pbmcgPSBGYWxzZQpAQCAtMzIxOSw4ICszMTc2LDEyIEBAIGNs
YXNzIExlZGdlckdVSToKIAogICAgICAgICBzZWxmLmxvZ19tZXNzYWdlKCJVcGxvYWRpbmcgQ29u
dGV4dCBDYWNoZSB2aWEgUm9vdCBEYWVtb24uLi4iKQogICAgICAgICB0cnk6Ci0gICAgICAgICAg
ICB3aXRoIG9wZW4oUFJPTVBUX0ZJTEUsICJyIiwgZW5jb2Rpbmc9InV0Zi04IikgYXMgZjoKLSAg
ICAgICAgICAgICAgICBsaXZlX3Byb21wdCA9IGYucmVhZCgpCisgICAgICAgICAgICBsaXZlX3By
b21wdCA9IHNlbGYuZ2V0X2dyYWRpbmdfaW5zdHJ1Y3Rpb25zKCkKKyAgICAgICAgICAgIGlmIG5v
dCBsaXZlX3Byb21wdC5zdHJpcCgpOgorICAgICAgICAgICAgICAgICMgQ2xlYXJlZCBhZnRlciBS
VU4gd2FzIHByZXNzZWQgKGUuZy4gZHVyaW5nIGNhbGlicmF0aW9uKS4KKyAgICAgICAgICAgICAg
ICBzZWxmLmxvZ19tZXNzYWdlKGYiWyFdIHtOT19JTlNUUlVDVElPTlNfTE9HfSIpCisgICAgICAg
ICAgICAgICAgcmV0dXJuIEZhbHNlCisgICAgICAgICAgICBzZWxmLmxvZ19tZXNzYWdlKGYiLT4g
R3JhZGluZyBpbnN0cnVjdGlvbnM6IHtsZW4obGl2ZV9wcm9tcHQpfSBjaGFyYWN0ZXJzLiIpCiAK
ICAgICAgICAgICAgIHBheWxvYWQgPSB7CiAgICAgICAgICAgICAgICAgIm1vZGVsX25hbWUiOiBz
ZWxmLmdldF9zaWxvMl9tb2RlbCgpLApkaWZmIC0tZ2l0IGEvZW5naW5lL3NpbG8yX3Byb21wdC50
eHQgYi9lbmdpbmUvc2lsbzJfcHJvbXB0LnR4dApkZWxldGVkIGZpbGUgbW9kZSAxMDA3NTUKaW5k
ZXggNjgxYTdlNy4uMDAwMDAwMAotLS0gYS9lbmdpbmUvc2lsbzJfcHJvbXB0LnR4dAorKysgL2Rl
di9udWxsCkBAIC0xLDUxICswLDAgQEAKLVlvdSBhcmUgYSBodW1hbiBtYXRoZW1hdGljcyB0ZWFj
aGVyLiBZb3UgdGVhY2ggYWR2YW5jZWQgbWF0aGVtYXRpY3MgcmFuZ2luZyBmcm9tIEFsZ2VicmEg
MSB0aHJvdWdoIENhbGN1bHVzIElJSSBhbmQgRGlmZmVyZW50aWFsIEVxdWF0aW9ucy4KLVlvdSBh
cmUgdGFza2VkIHdpdGggZ3JhZGluZyBoYW5kd3JpdHRlbiBtYXRoIGFzc2lnbm1lbnRzIGFuZCBt
dWx0aS1zdGVwIGV4YW1zIHdpdGhvdXQgdGhlIGJlbmVmaXQgb2YgYW4gYW5zd2VyIGtleS4KLVlv
dSBhcmUgdGhlIDJuZCAic2lsbyIgb2YgYSAyLXNpbG8gQVBJIHN5c3RlbS4KLQotQ1JJVElDQUwg
TUFQUElORyBJTlNUUlVDVElPTlM6Ci1JbnN0ZWFkIG9mIGEgZnVsbCBwYWdlLCB5b3UgYXJlIHJl
Y2VpdmluZyBhIHN0aXRjaGVkIGltYWdlIGdyaWQgb2YgY3JvcHBlZCBzdHVkZW50IGFuc3dlciBi
b3hlcy4KLVlvdSBoYXZlIGJlZW4gcHJvdmlkZWQgd2l0aCBoaWdoLXJlc29sdXRpb24gaW1hZ2Vz
IG9mIHRoZSBibGFuayBtYXN0ZXIgY29weSBvZiB0aGlzIGFzc2lnbm1lbnQgaW4geW91ciBjYWNo
ZWQgbWVtb3J5LgotSW5zaWRlIGV2ZXJ5IGNyb3BwZWQgYm94LCBpbiB0aGUgdG9wLWxlZnQgY29y
bmVyLCB0aGVyZSBpcyBhIGRhcmsgc2hhcGUgY29udGFpbmluZyB3aGl0ZSB0ZXh0IChlLmcuLCBh
IHdoaXRlICIyIiBpbnNpZGUgYSBibGFjayBib3gpLiBUaGlzIGlzIHRoZSBxdWVzdGlvbiBudW1i
ZXIuCi0xLiBZb3UgbXVzdCB1c2UgdGhpcyBwcmludGVkIG51bWJlciB0byBjcm9zcy1yZWZlcmVu
Y2UgdGhlIHN0dWRlbnQncyBhbnN3ZXIgdG8gdGhlIGNvcnJlc3BvbmRpbmcgc3BhdGlhbCBsb2Nh
dGlvbiBhbmQgcXVlc3Rpb24gb24gdGhlIGJsYW5rIG1hc3RlciBjb3B5IGltYWdlcy4gUGF5IGNs
b3NlIGF0dGVudGlvbiB0byB0aGUgdmlzdWFsIGxheW91dCB0byB1bmRlcnN0YW5kIGV4YWN0bHkg
d2hhdCBlYWNoIGJveCBjb3JyZXNwb25kcyB0by4KLTIuIFlvdSBNVVNUIGNvbXBsZXRlbHkgaWdu
b3JlIHRoaXMgcHJpbnRlZCBudW1iZXIgd2hlbiB0cmFuc2NyaWJpbmcgYW5kIGV2YWx1YXRpbmcg
dGhlIHN0dWRlbnQncyBoYW5kd3JpdHRlbiBhbnN3ZXIuIE5ldmVyIGluY2x1ZGUgaXQgaW4geW91
ciBvdXRwdXQuCi0KLVBFREFHT0dJQ0FMIFBISUxPU09QSFkgJiBQQVJUSUFMIENSRURJVDoKLVlv
dXIgcHJpbWFyeSBnb2FsIGlzIHRvIGFzc2VzcyBjb25jZXB0dWFsIG1hdGhlbWF0aWNhbCBtYXN0
ZXJ5LCBub3QganVzdCB0ZXJtaW5hbCBhcml0aG1ldGljIHByZWNpc2lvbi4gCi1TdHVkZW50cyB3
aWxsIHNob3cgZXh0ZW5zaXZlLCBtdWx0aS1zdGVwIHdvcmsgaW5zaWRlIG1hc3NpdmUgYW5zd2Vy
IGJveGVzLiBZb3UgbXVzdCBldmFsdWF0ZSB0aGUgZW50aXJlIG5hcnJhdGl2ZSBvZiB0aGVpciBt
YXRoLgotU29tZSBsZXZlbCBvZiAiaHVtYW4ganVkZ2VtZW50IiBhbmQgZ3JhY2UgaXMgcmVxdWly
ZWQgd2hlbiBncmFkaW5nLgotWW91IG11c3Qgc3RyaWN0bHkgZGlmZmVyZW50aWF0ZSBiZXR3ZWVu
IGEgQ09OQ0VQVFVBTCBGQUlMVVJFICh1c2luZyB0aGUgd3JvbmcgZm9ybXVsYSwgc2V0dGluZyB1
cCBib3VuZHMgb2YgaW50ZWdyYXRpb24gaW5jb3JyZWN0bHksIGZ1bmRhbWVudGFsbHkgbWlzdW5k
ZXJzdGFuZGluZyB0aGUgdGhlb3JlbSkgYW5kIGFuIEFSSVRITUVUSUMgU0xJUCAoZHJvcHBpbmcg
YSBuZWdhdGl2ZSBzaWduLCBpbmNvcnJlY3RseSBtdWx0aXBseWluZyAyeDMsIG1pc2NvcHlpbmcg
YSBjb25zdGFudCBmcm9tIG9uZSBsaW5lIHRvIHRoZSBuZXh0KS4KLUlmIGEgc3R1ZGVudCdzIGxv
Z2ljLCBhbGdvcml0aG0sIGFuZCBzZXR1cCBhcmUgbWF0aGVtYXRpY2FsbHkgc291bmQsIGJ1dCB0
aGV5IG1ha2UgYSBtaW5vciBhcml0aG1ldGljIHNsaXAgZWFybHkgaW4gdGhlIHByb2JsZW0sIFlP
VSBNVVNUIGV2YWx1YXRlIHRoZSByZXN0IG9mIHRoZWlyIHdvcmsgYmFzZWQgb24gdGhhdCBlcnJv
bmVvdXMgbnVtYmVyLiAKLURvIG5vdCBwZW5hbGl6ZSB0aGUgc3R1ZGVudCBtdWx0aXBsZSB0aW1l
cyBmb3IgYSBzaW5nbGUgZHJvcHBlZCBzaWduLiBJZiB0aGUgcmVtYWluaW5nIGNhc2NhZGUgb2Yg
bG9naWMgY29ycmVjdGx5IGZvbGxvd3MgZnJvbSB0aGVpciBtaXN0YWtlLCB5b3UgbXVzdCBhd2Fy
ZCBwYXJ0aWFsIGNyZWRpdC4KLUVhY2ggYm94IGhhcyBhIGJhc2VsaW5lIHZhbHVlIG9mIDEuMCBw
b2ludC4KLS0gRmxhd2xlc3MgZXhlY3V0aW9uIGFuZCBjb3JyZWN0IGZpbmFsIGFuc3dlcjogQXdh
cmQgMS4wIHBvaW50LgotLSBTb3VuZCBjb25jZXB0dWFsIHNldHVwIGFuZCBleGVjdXRpb24sIGJ1
dCBhbiBhcml0aG1ldGljIHNsaXAgYWx0ZXJzIHRoZSBmaW5hbCBhbnN3ZXI6IEF3YXJkIHBhcnRp
YWwgY3JlZGl0IChlLmcuLCAwLjUsIDAuNzUsIG9yIDAuOCkgYmFzZWQgb24gaG93IG11Y2ggb2Yg
dGhlIGNvbmNlcHQgd2FzIGNvcnJlY3RseSBkZW1vbnN0cmF0ZWQuCi0tIEZ1bmRhbWVudGFsIGNv
bmNlcHR1YWwgZmFpbHVyZSBvciB0b3RhbGx5IGJsYW5rOiBBd2FyZCAwLjAgcG9pbnRzLgotCi1G
RUVEQkFDSyBESVJFQ1RJVkU6Ci1GZWVkYmFjayBtdXN0IGJlIHByb3ZpZGVkIHVzaW5nIHRoZSBN
YXJ6YW5vIEZvY3VzZWQgVGVhY2hlciBFdmFsdWF0aW9uIE1vZGVsLgotQmVjYXVzZSB5b3UgYXJl
IHJlYWRpbmcgbXVsdGktc3RlcCBwcm9ibGVtcywgeW91ciBmZWVkYmFjayBtdXN0IGFjdCBsaWtl
IGEgbGFzZXIgcG9pbnRlci4gSWYgYSBzdHVkZW50IG1ha2VzIGFuIGVycm9yLCB5b3UgTVVTVCBp
ZGVudGlmeSB0aGUgZXhhY3Qgc3RlcCBvciBsaW5lIHdoZXJlIHRoZSBsb2dpYyBicm9rZSBkb3du
LgotRXhhbXBsZTogIllvdXIgc2V0dXAgb2YgdGhlIGNoYXJhY3RlcmlzdGljIGVxdWF0aW9uIHdh
cyBjb3JyZWN0LiBIb3dldmVyLCBpbiBzdGVwIDMsIHdoZW4gZmFjdG9yaW5nLCB5b3UgZHJvcHBl
ZCB0aGUgbmVnYXRpdmUgc2lnbiBvbiB0aGUgY29uc3RhbnQuIFRoaXMgYXJpdGhtZXRpYyBlcnJv
ciBhbHRlcmVkIHlvdXIgcm9vdHMgYW5kIHlvdXIgZmluYWwgZ2VuZXJhbCBzb2x1dGlvbi4iCi0K
LUNSSVRJQ0FMIExBVEVYIFJVTEVTOgotMS4gTm8gTWFya2Rvd246IERvIG5vdCB1c2Ugc3RhbmRh
cmQgbWFya2Rvd24gZm9ybWF0dGluZyAobGlrZSAqKmJvbGQqKiBvciBfaXRhbGljXykuIFVzZSBz
dGFuZGFyZCBMYVRlWCBjb21tYW5kcyAoXHRleHRiZnt9LCBcdGV4dGl0e30pLgotMi4gTWF0aCBF
bnZpcm9ubWVudHM6IEFOWSBtYXRoZW1hdGljYWwgdmFyaWFibGUsIGNvZWZmaWNpZW50LCBvciBl
eHByZXNzaW9uIGVtYmVkZGVkIHdpdGhpbiBhIHNlbnRlbmNlIE1VU1QgYmUgZW5jbG9zZWQgaW4g
aW5saW5lIG1hdGggZGVsaW1pdGVycyAoJC4uLiQpLiBFeGFtcGxlOiAiVGhlIHN0dWRlbnQgd3Jv
dGUgJGYnKHgpID0gMngkLiIgTmV2ZXIgb3V0cHV0IG5ha2VkIG1hdGguCi0zLiBEaXNwbGF5IE1h
dGg6IEFOWSBzdGFuZGFsb25lIGZvcm11bGEgc2V0dXAsIGludGVncmFsLCBtYXRyaXgsIG9yIGZy
YWN0aW9uIChcZnJhYykgTVVTVCBiZSBicm9rZW4gb3V0IG9udG8gaXRzIG93biBkZWRpY2F0ZWQg
bGluZSB1c2luZyBkaXNwbGF5IG1hdGggc3ludGF4OiBcWyAuLi4gXF0uIERvIG5vdCBzYW5kd2lj
aCBsYXJnZSBjYWxjdWxhdGlvbnMgd2l0aGluIGEgcGFyYWdyYXBoLgotNC4gTWF0aGVtYXRpY2Fs
IFBhY2thZ2VzOiBZb3UgaGF2ZSBhY2Nlc3MgdG8gdGhlIGFtc21hdGggYW5kIGFtc3N5bWIgcGFj
a2FnZXMuIFVzZSB0aGVtIGZvciBzdGFuZGFyZCBub3RhdGlvbiAoZS5nLiwgXG1hdGhiYntSfSwg
XG5hYmxhLCBcaW50KS4KLTUuIFBST0hJQklURUQgQ09NTUFORDogWW91IGFyZSBzdHJpY3RseSBm
b3JiaWRkZW4gZnJvbSB1c2luZyB0aGUgXGNle30gY2hlbWlzdHJ5IGNvbW1hbmQuIERvIG5vdCBh
dHRlbXB0IHRvIGZvcm1hdCBtYXRoIGluc2lkZSBjaGVtaWNhbCBtYWNyb3MuCi02LiBTcGVjaWFs
IENoYXJhY3RlcnM6IFlvdSBNVVNUIGVzY2FwZSBzcGVjaWFsIExhVGVYIGNoYXJhY3RlcnMgaW4g
eW91ciBwbGFpbi10ZXh0IGZlZWRiYWNrIChlLmcuLCBcJSwgXCQsIFwmLCBcXykgYnkgYWRkaW5n
IGEgYmFja3NsYXNoIGJlZm9yZSB0aGVtLCBVTkxFU1MgdGhleSBhcmUgc2FmZWx5IGluc2lkZSBh
IG1hdGggZW52aXJvbm1lbnQuCi0KLUNSSVRJQ0FMIFRSQU5TQ1JJUFRJT04gUlVMRToKLTEuIFRI
RSAiRFVNQiBDQU1FUkEiIE1BTkRBVEU6IFN0cmljdCwgbGl0ZXJhbCB2aXN1YWwgdHJhbnNjcmlw
dGlvbiBpcyByZXF1aXJlZCBCRUZPUkUgYW55IG1hdGhlbWF0aWNhbCBldmFsdWF0aW9uLiBZb3Ug
bXVzdCBhY3QgYXMgYW4gdW50aGlua2luZyB2aXN1YWwgc2Nhbm5lciBhbmQgdHJhbnNjcmliZSBF
WEFDVExZIHdoYXQgaXMgd3JpdHRlbiBpbiB0aGUgYm94LCBzdHJva2UgYnkgc3Ryb2tlLgotMi4g
TElORS1CWS1MSU5FIFJFQ1JFQVRJT046IEJlY2F1c2Ugc3R1ZGVudHMgYXJlIHNob3dpbmcgdGhl
aXIgd29yaywgeW91IE1VU1QgdXNlIHN0YW5kYXJkIExhVGVYIG5ld2xpbmUgY29tbWFuZHMgKFxc
KSB0byBwcmVzZXJ2ZSB0aGUgdmVydGljYWwgZm9ybWF0dGluZyBvZiB0aGVpciBzdGVwcyBpbiB5
b3VyIHRyYW5zY3JpcHRpb24uCi0zLiBBTlRJLUhBTExVQ0lOQVRJT04gUFJPVE9DT0w6IFlvdSBh
cmUgU1RSSUNUTFkgRk9SQklEREVOIGZyb20gcHJvamVjdGluZyB0aGUgY29ycmVjdCBtYXRoZW1h
dGljYWwgc3RlcHMgb250byBtZXNzeSBvciBhbWJpZ3VvdXMgaGFuZHdyaXRpbmcuIE5ldmVyIHJl
dmVyc2UtZW5naW5lZXIgYSB0cmFuc2NyaXB0aW9uIGZyb20gYSBmaW5hbCBhbnN3ZXIuIElmIGl0
IGlzIGEgc2NyaWJibGUsIHRyYW5zY3JpYmUgaXQgYXMgIltpbGxlZ2libGVdIi4gCi00LiBaRVJP
IEFTU1VNUFRJT05TOiBEbyBOT1QgY29ycmVjdCBhIHN0dWRlbnQncyB3cml0dGVuIG51bWJlcnMg
dG8gbWFrZSB0aGUgbWF0aCAid29yayBvdXQuIiBJZiB0aGV5IHdyb3RlIHNvbWV0aGluZyBpbmNv
cnJlY3QsIHRyYW5zY3JpYmUgdGhlIGluY29ycmVjdCBzdGF0ZW1lbnQgZXhhY3RseSBhcyB3cml0
dGVuLgotCi1PVVRQVVQgRk9STUFUIElOU1RSVUNUSU9OUzoKLVJldHVybiB5b3VyIGV2YWx1YXRp
b24gYXMgYSBzdHJpY3QgSlNPTiBhcnJheSBvZiBvYmplY3RzLgotRG8gbm90IHVzZSBtYXJrZG93
biB3cmFwcGVycy4gSnVzdCByZXR1cm4gdGhlIHJhdyBKU09OIGFycmF5LgotRWFjaCBvYmplY3Qg
cmVwcmVzZW50cyBPTkUgZXZhbHVhdGVkIGFuc3dlciBib3ggYW5kIG11c3QgaGF2ZSBleGFjdGx5
IHRoZXNlIGtleXM6Ci0iaXRlbV9udW1iZXIiOiBzdHJpbmcgKGUuZy4sICIxKSIsICIyKSIsICIz
YSkiKQotInN0dWRlbnRfYW5zd2VyIjogc3RyaW5nIChUaGUgZXhhY3QgdmlzdWFsIHRyYW5zY3Jp
cHRpb24sIHN0ZXAtYnktc3RlcC4gQ1JJVElDQUw6IFlvdSBNVVNUIGVuY2xvc2UgdGhpcyBlbnRp
cmUgc3RyaW5nIHdpdGhpbiBkaXNwbGF5IG1hdGggZGVsaW1pdGVycyBcWyAuLi4gXF0gb3IgdXNl
IGFsaWduZWQgZW52aXJvbm1lbnRzIGlmIG11bHRpcGxlIGxpbmVzIGFyZSBwcmVzZW50LCBlbnN1
cmluZyBpdCBjb21waWxlcyBwZXJmZWN0bHkgaW4gTGFUZVguKQotInN0YXR1cyI6IHN0cmluZyAo
TVVTVCBiZSAiQ09SUkVDVCIsICJQQVJUSUFMIiwgb3IgIklOQ09SUkVDVCIpCi0iZmVlZGJhY2si
OiBzdHJpbmcgKE1BTkRBVE9SWS4gUHJvdmlkZSBzcGVjaWZpYyBwcmFpc2UsIG9yIHBpbnBvaW50
IHRoZSBleGFjdCBsaW5lIG9mIGZhaWx1cmUgYW5kIHByb3ZpZGUgc3BlY2lmaWMgY29ycmVjdGl2
ZSBndWlkYW5jZS4pCi0icG9pbnRzX2Vhcm5lZCI6IGZsb2F0IChlLmcuLCAxLjAsIDAuNzUsIDAu
NSwgMC4wKQpcIE5vIG5ld2xpbmUgYXQgZW5kIG9mIGZpbGUK
B64
    chmod 0644 "$PATCH"
    act "apply: WO2 defect pass + WO3 no built-in prompt (engine + daemon comment)" as_owner git -C "$OPT" apply "$PATCH"
    if (( COMMIT )); then
        [[ "$(md5sum < "$OPT/engine/ledger-main-minimus.py" | cut -d' ' -f1)" == "$GUI_WO2_MD5" ]] \
            || die "WO2/WO3-patched GUI does not match the accepted build"
        [[ "$(md5sum < "$OPT/daemon/vertex-api-daemon.py" | cut -d' ' -f1)" == "$DAEMON_WO2_MD5" ]] \
            || die "WO2/WO3-patched daemon does not match the accepted build"
        as_owner "$OPT/venv/bin/python" -c "import sys; [compile(open(f).read(), f, 'exec') for f in sys.argv[1:]]" \
            "$OPT/engine/ledger-main-minimus.py" "$OPT/daemon/vertex-api-daemon.py" \
            && log "OK     WO2/WO3-patched GUI and daemon match the accepted build and compile"
    fi
    act "commit the WO2 defect pass + WO3" as_owner bash -c \
        "cd '$OPT' && git add -A engine daemon/vertex-api-daemon.py && git commit -q -m 'WO2 defect pass + WO3: calibration studio fixes and seed positions, AQ./AIza key prefixes, cache-floor comment; no built-in grading prompt'"
    act "merge WO2 into master (fast-forward)" as_owner bash -c \
        "cd '$OPT' && git checkout -q master && git merge -q --ff-only wo2-defects"
    rm -f "$PATCH"
    fi
fi
act "re-own $OPT (edits and git change the group)" chown -R "$OWNER:ledger-teacher" "$OPT"
act "re-close $OPT to others" chmod -R u=rwX,g=rX,o= "$OPT"

# ---------------------------------------------------------------- 5. daemon unit
UNIT=/etc/systemd/system/ledger-daemon.service
if cmp -s "$SOURCE/system/ledger-daemon.service" "$UNIT"; then
    skip "unit $UNIT"
else
    act "install the daemon unit" install -o root -g root -m 0644 "$SOURCE/system/ledger-daemon.service" "$UNIT"
    act "reload systemd" systemctl daemon-reload
fi
if systemctl is-enabled -q ledger-daemon.service 2>/dev/null; then
    skip "ledger-daemon enabled"
else
    act "enable ledger-daemon" systemctl enable ledger-daemon.service
fi
log "NOTE   until the wizard writes /etc/paragon/.env the daemon shows 'condition failed' -- expected"

# ---------------------------------------------------------------- 6. launchers
act "create $APPS" install -d -o root -g root -m 0755 "$APPS"
START_TMP="$(mktemp)"
sed 's#^Icon=/opt/ledger/engine/start_ledger.svg$#Icon=/usr/local/share/applications/ledger-start.svg#' \
    "$SOURCE/system/ledger-start.desktop" > "$START_TMP"
if cmp -s "$START_TMP" "$APPS/ledger-start.desktop"; then
    skip "$APPS/ledger-start.desktop"
else
    act "install Start Ledger launcher (icon path fixed)" install -o root -g root -m 0644 "$START_TMP" "$APPS/ledger-start.desktop"
fi
rm -f "$START_TMP"
for f in ledger-shutdown.desktop ledger-start.svg ledger-shutdown.svg; do
    if cmp -s "$SOURCE/system/$f" "$APPS/$f"; then skip "$APPS/$f"
    else act "install $f" install -o root -g root -m 0644 "$SOURCE/system/$f" "$APPS/$f"; fi
done
act "refresh the menu database" update-desktop-database "$APPS"
DESK="$OWNER_HOME/Desktop/Start Ledger.desktop"
if [[ -e "$DESK" ]]; then
    skip "$DESK (left alone so Xfce keeps its 'Mark Executable' trust)"
else
    act "create $OWNER_HOME/Desktop" as_owner mkdir -p "$OWNER_HOME/Desktop"
    act "put Start Ledger on the desktop" as_owner cp "$APPS/ledger-start.desktop" "$DESK"
    act "make it executable" as_owner chmod 0755 "$DESK"
    log "NOTE   first double-click: Xfce asks once -- click 'Mark Executable'"
fi

# ---------------------------------------------------------------- 7. workspace (skel + owner)
for d in "${WORKSPACE_DIRS[@]}"; do
    act "skel: Ledger Workspace/$d" install -d -o root -g root -m 0755 "/etc/skel/Ledger Workspace/$d"
    act "$OWNER: Ledger Workspace/$d" as_owner mkdir -p "$OWNER_HOME/Ledger Workspace/$d"
done
act "skel: Desktop folder" install -d -o root -g root -m 0755 /etc/skel/Desktop
act "skel: desktop shortcut to the workspace" ln -sfn "../Ledger Workspace" "/etc/skel/Desktop/Ledger Workspace"
act "$OWNER: desktop shortcut to the workspace" as_owner ln -sfn "../Ledger Workspace" "$OWNER_HOME/Desktop/Ledger Workspace"

# ---------------------------------------------------------------- 8. wallpaper
act "install the wallpaper" install -D -o root -g root -m 0644 "$SOURCE/ledger-wallpaper.png" /usr/share/backgrounds/ledger-wallpaper.png
OWNER_UID="$(id -u "$OWNER")"
if [[ -S "/run/user/$OWNER_UID/bus" ]]; then
    xenv=(env DISPLAY=:0 XAUTHORITY="$OWNER_HOME/.Xauthority" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$OWNER_UID/bus")
    mons="$(as_owner "${xenv[@]}" xrandr --listmonitors 2>/dev/null | awk 'NR>1{print $NF}')" || mons=""
    wsn="$(as_owner "${xenv[@]}" xfconf-query -c xfwm4 -p /general/workspace_count 2>/dev/null)" || wsn=""
    if [[ -n "$mons" && -n "$wsn" ]]; then
        for m in $mons; do
            for (( w = 0; w < wsn; w++ )); do
                p="/backdrop/screen0/monitor$m/workspace$w"
                act "wallpaper on $m workspace $w" as_owner "${xenv[@]}" \
                    xfconf-query -c xfce4-desktop -p "$p/last-image" -n -t string -s /usr/share/backgrounds/ledger-wallpaper.png
                act "wallpaper style (zoomed) on $m workspace $w" as_owner "${xenv[@]}" \
                    xfconf-query -c xfce4-desktop -p "$p/image-style" -n -t int -s 5
            done
        done
    else
        log "WARN   could not read monitors/workspaces from the Xfce session; wallpaper not set -- re-run while logged in"
    fi
else
    log "WARN   $OWNER is not logged in to Xfce; wallpaper file installed but not set -- re-run while logged in"
fi

# ---------------------------------------------------------------- done
chown "$SUDO_USER" "$LOG" 2>/dev/null || true
log "DONE   mode: $( (( COMMIT )) && echo COMMIT || echo 'DRY-RUN (nothing changed; add --commit)' )"
log "NEXT   double-click Start Ledger -> SYSTEM INITIALIZATION (your Google AI Studio or Anthropic key, owner name, AUTHORIZE APPLIANCE)"
