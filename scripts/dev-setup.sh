#!/bin/bash
# Prepare a fresh machine to run SpinSense's test suite.
#
# Written for cloud/sandbox environments where the default assumption is Node
# (there is no package.json — this project is Python) and where the two audio
# dependencies may be unbuildable. Safe to re-run.
#
# CI remains the authority: it installs the exact pins on Python 3.11, which is
# what docker/Dockerfile ships. This script's job is only to get a suite running
# wherever it is pointed, and to say plainly when it couldn't.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
PIP="python3 -m pip install --quiet"

# PortAudio is the one hard blocker. sounddevice loads it at *import* time and
# both core_engine and backend_main import it at module scope, so without it
# every test in core/ and gui/ fails at collection rather than failing a test.
if command -v apt-get >/dev/null 2>&1; then
  (sudo -n apt-get update -qq || apt-get update -qq) >/dev/null 2>&1
  (sudo -n apt-get install -y --no-install-recommends libportaudio2 \
     || apt-get install -y --no-install-recommends libportaudio2) >/dev/null 2>&1
fi

# The pins target Python 3.11. On anything newer, numpy and Pillow have no
# matching wheel and fall back to building from source, which needs headers this
# kind of box rarely has — so relax those rather than fail. Acceptable because
# the suite is pure logic; CI still checks the real pins.
$PIP --upgrade pip
if ! $PIP -r requirements-dev.txt -c constraints.txt; then
  echo "note: pinned install failed (Python newer than 3.11?) — relaxing versions"
  # Relaxed means *different*, not just newer: the image runs pydantic 1.x
  # (shazamio requires it) and this path installs 2.x. Code written against
  # what is installed here can pass every test and still not run in the image.
  echo "note: this is NOT what ships — pydantic 2.x here, 1.x in the image; CI decides"
  $PIP numpy Pillow aiohttp fastapi "uvicorn[standard]" jinja2 zeroconf \
       httpx pytest ruff vulture
  $PIP sounddevice shazamio || echo "note: audio libraries unavailable — stubbing"
fi

# shazamio-core needs a Rust toolchain where no wheel exists, and PortAudio is
# not always installable. Both are reached only through calls that every test
# monkeypatches, so stub them rather than lose the suite. Written *only* when
# the real import fails, so a working install is never shadowed.
STUBS="${SPINSENSE_TEST_STUBS:-$HOME/.spinsense-test-stubs}"
mkdir -p "$STUBS"
python3 - "$STUBS" <<'PY'
import pathlib, sys

stubs = pathlib.Path(sys.argv[1])


def missing(module):
    try:
        __import__(module)
        return False
    except Exception:
        return True


if missing("sounddevice"):
    (stubs / "sounddevice.py").write_text(
        '"""Stub: PortAudio is unavailable here. Tests monkeypatch every call."""\n'
        "class InputStream:\n"
        "    def __init__(self, **kw): self.kw = kw\n"
        "    def start(self): pass\n"
        "    def stop(self): pass\n"
        "    def close(self): pass\n"
        "def rec(*a, **kw): raise RuntimeError('stub sounddevice.rec')\n"
        "def wait(*a, **kw): pass\n"
        "def query_devices(*a, **kw): return []\n")
    print("stubbed sounddevice")
if missing("shazamio"):
    (stubs / "shazamio.py").write_text(
        '"""Stub: shazamio-core needs a Rust toolchain here."""\n'
        "class Shazam:\n"
        "    async def recognize(self, *a, **kw):\n"
        "        raise RuntimeError('stub Shazam.recognize')\n")
    print("stubbed shazamio")
PY

# Stubs are inert unless they are on the import path. Exported here so the check
# below means something; set PYTHONPATH in the environment config too, so every
# session gets it and not just this script.
export PYTHONPATH="$STUBS${PYTHONPATH:+:$PYTHONPATH}"

# Report whether the suite can actually run, so a broken environment is obvious
# now rather than three tool calls into the first task.
echo "--- setup check ---"
python3 -c "import sys; print('python', sys.version.split()[0])"
ok=0
for d in spinsense core gui; do
  if ( cd "$d" && python3 -m pytest -q --collect-only >/dev/null 2>&1 ); then
    echo "$d: collects"
  else
    echo "$d: COLLECTION FAILED — run 'cd $d && python3 -m pytest' to see why"
    ok=1
  fi
done
if [ "$ok" = 0 ]; then
  echo
  echo "Run the suites with:  for d in spinsense core gui; do (cd \$d && python3 -m pytest -q); done"
  echo "Lint with:            ruff check --select E4,E7,E9,F core/ gui/ spinsense/"
  echo "                      vulture core/ gui/ spinsense/ --min-confidence 100"
  echo "                      npx --yes eslint@10.12.0 gui/static   (needs Node)"
fi
exit 0
