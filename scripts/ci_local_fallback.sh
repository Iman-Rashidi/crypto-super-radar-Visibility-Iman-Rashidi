#!/usr/bin/env bash
# Secret-isolated local fallback for .github/workflows/offline-tests.yml.
# No exchange credentials, live order endpoints, or production database are used.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3.14}"
ALLOW_NON_314="${ALLOW_NON_314:-0}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "ERROR: $PYTHON_BIN was not found. Install CPython 3.14 or set PYTHON_BIN to its executable." >&2
  exit 2
fi

PYTHON_VERSION="$("$PYTHON_BIN" -c 'import platform; print(platform.python_version())')"
PYTHON_314_MATCH=1
if [[ "$PYTHON_VERSION" != 3.14.* ]]; then
  PYTHON_314_MATCH=0
  if [[ "$ALLOW_NON_314" != 1 ]]; then
    echo "ERROR: this workflow targets Python 3.14; found $PYTHON_VERSION." >&2
    echo "For a non-acceptance smoke run only, set ALLOW_NON_314=1 and PYTHON_BIN explicitly." >&2
    exit 2
  fi
  echo "WARNING: continuing on Python $PYTHON_VERSION; this is not Python 3.14 CI evidence." >&2
fi

for command_name in tar curl grep; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "ERROR: required command not found: $command_name" >&2
    exit 2
  }
done

ARCHIVE="$ROOT/dist/crypto-super-radar-source.tar.xz"
if [[ ! -f "$ARCHIVE" ]]; then
  echo "ERROR: required source bundle is missing: $ARCHIVE" >&2
  exit 2
fi

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/csr-ci-fallback.XXXXXX")"
mkdir -p "$TMP_DIR/home" "$TMP_DIR/render-overlay" "$TMP_DIR/full-bundle"
ARCHIVE_BACKUP="$TMP_DIR/source-archive.original.tar.xz"
cp "$ARCHIVE" "$ARCHIVE_BACKUP"

cleanup() {
  local exit_code=$?
  trap - EXIT
  if [[ -f "$ARCHIVE_BACKUP" ]]; then
    cp -f "$ARCHIVE_BACKUP" "$ARCHIVE" || exit_code=1
  fi
  if [[ "$exit_code" -ne 0 && -f "$TMP_DIR/server.log" ]]; then
    echo "--- local web smoke server log (tail) ---" >&2
    tail -n 80 "$TMP_DIR/server.log" >&2 || true
  fi
  rm -rf "$TMP_DIR"
  exit "$exit_code"
}
trap cleanup EXIT

# Project processes receive an empty environment and temporary HOME. This
# prevents inherited API keys, database credentials and local tool config
# from reaching the test process.
clean_env() {
  env -i \
    PATH="$PATH" \
    HOME="$TMP_DIR/home" \
    TMPDIR="$TMP_DIR" \
    LANG=C.UTF-8 \
    PIP_CONFIG_FILE=/dev/null \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONNOUSERSITE=1 \
    "$@"
}

echo "Python: $PYTHON_VERSION"
echo "Creating a temporary virtual environment and installing test dependencies..."
clean_env "$PYTHON_BIN" -m venv "$TMP_DIR/venv"
PYTHON="$TMP_DIR/venv/bin/python"
clean_env "$PYTHON" -m pip install --upgrade pip
clean_env "$PYTHON" -m pip install numpy pandas requests "streamlit>=1.40" -r "$ROOT/requirements.txt"

echo "[1/5] Compile source and tests..."
clean_env "$PYTHON" -m compileall -q "$ROOT/crypto_super_radar" "$ROOT/tests"

echo "[2/5] Rebuild source archive (the tracked original is restored on exit)..."
clean_env "$PYTHON" "$ROOT/scripts/rebuild_source_archive.py"

echo "[3/5] Verify Render overlay build and localhost HTTP startup..."
tar -xJf "$ARCHIVE" -C "$TMP_DIR/render-overlay"
RENDER_ROOT="$TMP_DIR/render-overlay/crypto-super-radar"
cp -R "$ROOT/crypto_super_radar/." "$RENDER_ROOT/crypto_super_radar/"
cp "$ROOT/README.md" "$ROOT/pyproject.toml" "$ROOT/requirements.txt" "$RENDER_ROOT/"
(
  cd "$RENDER_ROOT"
  clean_env "$PYTHON" -m pip install -r requirements.txt
  clean_env "$PYTHON" -m compileall -q .
  PORT="$(clean_env "$PYTHON" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
  clean_env PORT="$PORT" CSR_DB_PATH="$TMP_DIR/render-overlay/csr-test.sqlite3" \
    "$PYTHON" -m crypto_super_radar.web_app > "$TMP_DIR/server.log" 2>&1 &
  server_pid=$!
  trap 'kill "$server_pid" 2>/dev/null || true' EXIT
  for attempt in $(seq 1 30); do
    if curl --connect-timeout 1 -fsS "http://127.0.0.1:$PORT/health" > "$TMP_DIR/health.json"; then
      break
    fi
    sleep 0.5
  done
  grep -q '"status":"ok"' "$TMP_DIR/health.json"
  curl --connect-timeout 2 -fsS "http://127.0.0.1:$PORT/" > "$TMP_DIR/home.html"
  grep -q '۱۲ فیلتر اجباری' "$TMP_DIR/home.html"
  curl --connect-timeout 2 -fsS "http://127.0.0.1:$PORT/history?asset=cex%3ABTCUSDT" > "$TMP_DIR/history.json"
  grep -q 'LOCAL_SQLITE' "$TMP_DIR/history.json"
)

echo "[4/5] Run the full extracted-bundle regression and CLI checks..."
tar -xJf "$ARCHIVE" -C "$TMP_DIR/full-bundle"
FULL_ROOT="$TMP_DIR/full-bundle/crypto-super-radar"
(
  cd "$FULL_ROOT"
  clean_env "$PYTHON" -m compileall -q .
  clean_env "$PYTHON" -m unittest discover -s tests -v
  clean_env "$PYTHON" -m crypto_super_radar --help >/dev/null
  clean_env "$PYTHON" -m crypto_super_radar.order_flow_service --help >/dev/null
  clean_env "$PYTHON" scripts/paper_trading_bot.py --help >/dev/null
)

echo "[5/5] Smoke-test the optional Streamlit dashboard..."
(
  cd "$FULL_ROOT"
  clean_env "$PYTHON" - <<'PY'
from streamlit.testing.v1 import AppTest
app = AppTest.from_file("crypto_super_radar/streamlit_app.py", default_timeout=20).run()
assert not app.exception, app.exception
assert any("CRYPTO SUPER RADAR" in item.value for item in app.title)
assert any("اجرای اسکن پژوهشی" in item.label for item in app.button)
PY
)

if [[ "$PYTHON_314_MATCH" -eq 1 ]]; then
  echo "LOCAL CI FALLBACK: all offline workflow checks completed on Python 3.14."
else
  echo "LOCAL SMOKE ONLY: checks completed on Python $PYTHON_VERSION; Python 3.14 CI remains unverified."
fi
