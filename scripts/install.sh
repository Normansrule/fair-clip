#!/usr/bin/env bash
# Install clipwarden as an app for this user (Ubuntu / Debian / WSL2). No sudo, no build step.
#
#   From a clone:        bash scripts/install.sh
#   Straight from GitHub (latest release, checksum-verified):
#     curl -fsSL https://raw.githubusercontent.com/Normansrule/clipwarden/main/scripts/install.sh | bash
#   A specific release:  CLIPWARDEN_VERSION=v0.2.0 bash install.sh
#   Remove it again:     bash scripts/install.sh --uninstall   (your data in ~/.local/share/clipwarden is kept)
#
# What it does: puts the code in ~/.local/share/clipwarden/app, installs hash-pinned dependencies
# into its own virtual environment, adds a `clipwarden` command to ~/.local/bin and a launcher to
# your applications menu (on WSL2 with WSLg it shows up in the Windows Start menu).
set -euo pipefail

REPO="${CLIPWARDEN_REPO:-Normansrule/clipwarden}"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/clipwarden"
APP="$DATA/app"
BIN="$HOME/.local/bin"
DESKTOP="${XDG_DATA_HOME:-$HOME/.local/share}/applications/clipwarden.desktop"

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -rf "$APP" "$BIN/clipwarden" "$DESKTOP"
  say "removed the app. Your data and config are still in $DATA (delete that folder to remove them too)."
  exit 0
fi

# ---------------------------------------------------------------- requirements
command -v python3 >/dev/null || die "python3 not found: sudo apt install -y python3 python3-venv"
python3 - <<'PY' || die "Python 3.11 or newer is required"
import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY
python3 -c "import venv, ensurepip" 2>/dev/null || die "python3-venv missing: sudo apt install -y python3-venv"
command -v ffmpeg >/dev/null || warn "ffmpeg not found (needed to render clips): sudo apt install -y ffmpeg"
command -v docker >/dev/null || warn "docker not found: your own videos are only processed inside the network-less Docker sandbox (see README, Security)"

# ---------------------------------------------------------------- get the code
SRC=""
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [[ -n "$HERE" && -f "$HERE/../pyproject.toml" && -d "$HERE/../src/clipwarden" ]]; then
  SRC="$(cd "$HERE/.." && pwd)"
  VERSION="$(git -C "$SRC" describe --tags --always 2>/dev/null || echo local)"
  say "installing from this checkout ($SRC, $VERSION)"
else
  command -v curl >/dev/null || die "curl not found: sudo apt install -y curl"
  VERSION="${CLIPWARDEN_VERSION:-}"
  if [[ -z "$VERSION" ]]; then
    VERSION="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])')" \
      || die "could not find a release of $REPO (set CLIPWARDEN_VERSION=vX.Y.Z)"
  fi
  [[ "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "unexpected version '$VERSION'"
  TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
  base="https://github.com/$REPO/releases/download/$VERSION"
  say "downloading clipwarden $VERSION"
  curl -fsSL -o "$TMP/clipwarden-$VERSION.tar.gz" "$base/clipwarden-$VERSION.tar.gz"
  curl -fsSL -o "$TMP/SHA256SUMS" "$base/SHA256SUMS"
  (cd "$TMP" && grep " clipwarden-$VERSION.tar.gz\$" SHA256SUMS | sha256sum -c -) || die "checksum mismatch: refusing to install"
  if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
    gh attestation verify "$TMP/clipwarden-$VERSION.tar.gz" -R "$REPO" >/dev/null && say "build provenance verified (Sigstore)" \
      || die "provenance check failed: refusing to install"
  fi
  tar -xzf "$TMP/clipwarden-$VERSION.tar.gz" -C "$TMP"
  SRC="$TMP/clipwarden-$VERSION"
fi

# ---------------------------------------------------------------- install
mkdir -p "$DATA" "$BIN" "$(dirname "$DESKTOP")"
chmod 700 "$DATA"
rm -rf "$APP.new"
mkdir -p "$APP.new"
cp -r "$SRC/src" "$SRC/examples" "$SRC/requirements-youtube.lock" "$SRC/LICENSE" "$APP.new/"
cp "$SRC/scripts/install.sh" "$APP.new/install.sh"
[[ -d "$SRC/sandbox" ]] && cp -r "$SRC/sandbox" "$APP.new/"
echo "$VERSION" > "$APP.new/VERSION"
say "creating a private Python environment with hash-pinned dependencies (about a minute)"
python3 -m venv "$APP.new/.venv"
"$APP.new/.venv/bin/pip" install --quiet --disable-pip-version-check --require-hashes -r "$APP.new/requirements-youtube.lock"
rm -rf "$APP"
mv "$APP.new" "$APP"

cat > "$BIN/clipwarden" <<EOF
#!/bin/sh
# clipwarden $VERSION launcher (installed by scripts/install.sh)
PYTHONPATH="$APP/src" exec "$APP/.venv/bin/python" -m clipwarden.cli "\$@"
EOF
chmod 755 "$BIN/clipwarden"

cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=clipwarden
Comment=Review, approve and publish rights-cleared clips (runs on this computer)
Exec=$BIN/clipwarden app
Icon=$APP/src/clipwarden/web/static/icon-512.png
Terminal=false
Categories=AudioVideo;Video;
EOF

say "installed clipwarden $VERSION"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) warn "$BIN is not on your PATH yet. Run:  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc && source ~/.bashrc" ;;
esac
echo
echo "  Start it:   clipwarden app        (opens the dashboard in your browser)"
echo "  Try it:     clipwarden --help"
echo "  Update it:  run this installer again"
echo "  Remove it:  bash $APP/install.sh --uninstall"
