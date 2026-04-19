#!/usr/bin/env bash
# bringup.sh — assert the dev environment is ready before you start coding.
#
# Run this from the repo root before each session:
#
#     ./bringup.sh
#
# It is *idempotent and read-mostly*: it only writes to disk when it has
# to (creating the venv, installing requirements). On a healthy system
# it just prints checkmarks and exits 0. On an unhealthy system it
# prints a diagnosis and exits non-zero so CI / agents can catch it.
#
# What it guards against (the real failure modes we've hit):
#   1. Stale system yt-dlp shadowing the venv yt-dlp
#      (every Python process still picked /usr/bin/yt-dlp via $PATH
#      because .venv/bin was never on $PATH; downloads broke when
#      YouTube rotated player JS).
#   2. Missing Deno (yt-dlp's nsig solver silently degrades).
#   3. Missing/expired YouTube cookies file.
#   4. requirements.txt drift (someone added a dep but forgot to pip
#      install in the venv).
#
# Exit codes:
#   0  - everything OK (or fixed)
#   1  - a hard failure that needs the human (missing system pkg,
#        expired cookies, ...)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

# ---- pretty output --------------------------------------------------
if [[ -t 1 ]]; then
  GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; DIM=$'\033[2m'; RESET=$'\033[0m'
else
  GREEN=""; RED=""; YELLOW=""; DIM=""; RESET=""
fi
ok()    { echo "  ${GREEN}✓${RESET} $*"; }
warn()  { echo "  ${YELLOW}!${RESET} $*"; }
fail()  { echo "  ${RED}✗${RESET} $*"; FAILED=1; }
section(){ echo; echo "${DIM}=== $* ===${RESET}"; }

FAILED=0
VENV="$REPO_ROOT/.venv"
VENV_BIN="$VENV/bin"
VENV_PY="$VENV_BIN/python"
VENV_PIP="$VENV_BIN/pip"

# ---- 1. Python + venv ----------------------------------------------
section "Python venv"

if ! command -v python3 >/dev/null 2>&1; then
  fail "python3 not on PATH (apt install python3 python3-venv)"
else
  ok "python3: $(python3 --version 2>&1) ($(command -v python3))"
fi

if [[ ! -x "$VENV_PY" ]]; then
  warn "no venv at $VENV — creating one"
  python3 -m venv "$VENV"
  ok "venv created"
else
  ok "venv present at $VENV"
fi

ok "venv python: $("$VENV_PY" --version 2>&1)"

# ---- 2. Python deps -------------------------------------------------
section "Python dependencies"

# Install / update requirements if anything in requirements.txt is
# missing or older than required. pip's resolver is the cheapest way
# to assert this; it's a no-op when everything matches.
if ! "$VENV_PIP" install --quiet -r requirements.txt; then
  fail "pip install -r requirements.txt failed"
else
  ok "requirements.txt satisfied"
fi

# requirements-dev is optional — only install if it's there
if [[ -f requirements-dev.txt ]]; then
  if "$VENV_PIP" install --quiet -r requirements-dev.txt; then
    ok "requirements-dev.txt satisfied"
  else
    warn "could not install requirements-dev.txt (dev tools may be missing)"
  fi
fi

# ---- 3. yt-dlp version sanity (the big one) ------------------------
section "yt-dlp resolution (the gotcha)"

VENV_YTDLP="$VENV_BIN/yt-dlp"
SYS_YTDLP="$(command -v yt-dlp 2>/dev/null || true)"

if [[ ! -x "$VENV_YTDLP" ]]; then
  fail "$VENV_YTDLP missing — pip install of yt-dlp failed?"
else
  V_VEN="$("$VENV_YTDLP" --version 2>/dev/null || echo unknown)"
  ok "venv yt-dlp:   $V_VEN  ($VENV_YTDLP)"
fi

if [[ -z "$SYS_YTDLP" ]]; then
  ok "no system yt-dlp on PATH (good — nothing to shadow the venv)"
elif [[ "$SYS_YTDLP" == "$VENV_YTDLP" ]]; then
  ok "PATH yt-dlp resolves to the venv binary"
else
  V_SYS="$("$SYS_YTDLP" --version 2>/dev/null || echo unknown)"
  warn "PATH yt-dlp is $SYS_YTDLP (version $V_SYS), NOT the venv"
  warn "   the app pins .venv/bin/yt-dlp internally, so this is OK,"
  warn "   but a manual \`yt-dlp ...\` from your shell will use $V_SYS."
  warn "   Activate the venv (\`source .venv/bin/activate\`) for shell parity."
fi

# Make sure the app actually resolves to the venv binary the way
# downloader._yt_dlp_path does. Fail loudly if it ever drifts.
RESOLVED="$("$VENV_PY" -c "from app.services.downloader import _yt_dlp_path; print(_yt_dlp_path() or '')" 2>/dev/null || true)"
if [[ -z "$RESOLVED" ]]; then
  fail "downloader._yt_dlp_path() returned nothing — import failure?"
elif [[ "$RESOLVED" != "$VENV_YTDLP" ]]; then
  fail "downloader._yt_dlp_path() resolves to $RESOLVED, expected $VENV_YTDLP"
  fail "   (this is exactly the bug bringup.sh exists to catch)"
else
  ok "downloader._yt_dlp_path() -> $RESOLVED"
fi

# ---- 4. Deno (yt-dlp nsig solver) ----------------------------------
section "Deno (required for YouTube nsig)"

DENO="$(command -v deno || true)"
if [[ -z "$DENO" && -x "$HOME/.local/bin/deno" ]]; then
  DENO="$HOME/.local/bin/deno"
  warn "deno not on \$PATH but found at $DENO"
  warn "   add this to your shell rc: export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

if [[ -z "$DENO" ]]; then
  fail "deno not installed; install with:"
  fail "   curl -fsSL https://deno.land/install.sh | sh"
  fail "   then: export PATH=\"\$HOME/.local/bin:\$PATH\""
else
  DV="$("$DENO" --version 2>/dev/null | head -1 || echo unknown)"
  ok "deno: $DV ($DENO)"
fi

# ---- 5. ffmpeg (yt-dlp muxer) --------------------------------------
section "ffmpeg (yt-dlp muxer)"

if ! command -v ffmpeg >/dev/null 2>&1; then
  fail "ffmpeg missing (sudo apt install ffmpeg)"
else
  ok "ffmpeg: $(ffmpeg -version 2>/dev/null | head -1)"
fi

# ---- 6. YouTube cookies --------------------------------------------
section "YouTube cookies"

COOKIES="${PI_HUB_YT_COOKIES:-$REPO_ROOT/secrets/youtube-cookies.txt}"
if [[ ! -f "$COOKIES" ]]; then
  fail "cookies file missing: $COOKIES"
  fail "   downloads will hit 'Sign in to confirm you're not a bot'."
  fail "   See README — export Netscape cookies.txt from a logged-in"
  fail "   YouTube session and put it at the path above."
else
  AGE_DAYS=$(( ( $(date +%s) - $(stat -c %Y "$COOKIES") ) / 86400 ))
  if [[ "$AGE_DAYS" -gt 7 ]]; then
    warn "cookies file is $AGE_DAYS days old at $COOKIES"
    warn "   YouTube rotates auth aggressively; consider re-exporting if"
    warn "   downloads start failing with 'rejected the cookies'."
  else
    ok "cookies file: $COOKIES (age ${AGE_DAYS}d)"
  fi
  # Sanity-check it actually has youtube cookies in it (it's a Netscape
  # cookies.txt — no JSON parsing needed).
  if grep -q -E "(^| )\.?youtube\.com" "$COOKIES" 2>/dev/null; then
    ok "cookies file contains youtube.com entries"
  else
    warn "cookies file has no youtube.com entries — wrong export?"
  fi
fi

# ---- 7. Media directories -------------------------------------------
section "Media directories"

for d in media/videos media/music; do
  if [[ -d "$d" ]]; then
    ok "$d/ exists"
  else
    warn "$d/ missing — creating"
    mkdir -p "$d"
    ok "$d/ created"
  fi
done

# ---- summary --------------------------------------------------------
echo
if [[ "$FAILED" -ne 0 ]]; then
  echo "${RED}bringup failed.${RESET} Fix the items marked ✗ above."
  exit 1
fi

echo "${GREEN}bringup OK.${RESET} You're good to go."
echo
echo "Common next steps:"
echo "  .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000   # web UI"
echo "  .venv/bin/python scripts/bulk_download.py video bulk.txt    # bulk DL"
echo "  source .venv/bin/activate                                   # shell parity"
