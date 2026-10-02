#!/usr/bin/env bash
# Install clipwarden as an app for this user (Ubuntu / Debian / WSL2). No sudo, no build step.
#
#   Latest release, checksum-verified (the usual way):
#     curl -fsSL https://raw.githubusercontent.com/Normansrule/clipwarden/main/scripts/install.sh | bash
#   A specific release:   curl -fsSL .../install.sh | CLIPWARDEN_VERSION=v0.2.0 bash
#   From a clone you trust (development):   bash scripts/install.sh --from-checkout
#   Remove it:            bash ~/.local/share/clipwarden/app/install.sh --uninstall   (keeps your data)
#
# What it does: puts the code in ~/.local/share/clipwarden/versions/<version>, points
# ~/.local/share/clipwarden/app at it, installs hash-pinned dependencies into a private virtual
# environment, and adds a `clipwarden` command to ~/.local/bin plus a launcher in your apps menu
# (on WSL2 with WSLg it shows up in the Windows Start menu).
#
# Everything is inside main(), called on the last line: if the download is cut off part-way,
# bash never runs a partial script.
set -euo pipefail
TMP=""

main() {
  local REPO="${CLIPWARDEN_REPO:-Normansrule/clipwarden}"
  local DATA="${XDG_DATA_HOME:-$HOME/.local/share}/clipwarden"
  local APP="$DATA/app"
  local BIN="$HOME/.local/bin"
  local DESKTOP="${XDG_DATA_HOME:-$HOME/.local/share}/applications/clipwarden.desktop"
  local MARK="# clipwarden launcher (installed by scripts/install.sh)"
  local mode="release"

  case "${1:-}" in
    --uninstall) uninstall; return ;;
    --from-checkout) mode="checkout" ;;
    "") ;;
    *) die "unknown option '$1' (use --from-checkout or --uninstall)" ;;
  esac

  # ------------------------------------------------------------ requirements
  command -v python3 >/dev/null || die "python3 not found: sudo apt install -y python3 python3-venv"
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || die "Python 3.11 or newer is required"
  python3 -c "import venv, ensurepip" 2>/dev/null || die "python3-venv missing: sudo apt install -y python3-venv"
  command -v ffmpeg >/dev/null || warn "ffmpeg not found (needed to render clips): sudo apt install -y ffmpeg"
  command -v docker >/dev/null || warn "docker not found: your own videos are only processed inside the network-less Docker sandbox (README → Security)"

  # ------------------------------------------------------------ get the code
  local SRC VERSION
  TMP="$(mktemp -d)"  # global on purpose: the EXIT trap runs after main() returns
  trap 'if [[ -n "${TMP:-}" ]]; then rm -rf -- "$TMP"; fi' EXIT
  if [[ "$mode" == "checkout" ]]; then
    # Only when run as a real file (never when piped), and only from the checkout it lives in.
    local self="${BASH_SOURCE[0]:-}"
    [[ -n "$self" && -f "$self" ]] || die "--from-checkout needs the script file: bash scripts/install.sh --from-checkout"
    SRC="$(cd "$(dirname "$self")/.." && pwd)"
    [[ -f "$SRC/pyproject.toml" && -d "$SRC/src/clipwarden" ]] || die "$SRC is not a clipwarden checkout"
    VERSION="$(git -C "$SRC" describe --tags --always --dirty 2>/dev/null || echo local)"
    say "installing from your checkout ($SRC, $VERSION): no download, no checksum step"
  else
    command -v curl >/dev/null || die "curl not found: sudo apt install -y curl"
    VERSION="${CLIPWARDEN_VERSION:-}"
    if [[ -z "$VERSION" ]]; then
      VERSION="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])')" \
        || die "could not find a release of $REPO (set CLIPWARDEN_VERSION=vX.Y.Z)"
    fi
    [[ "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "unexpected version '$VERSION'"
    local base="https://github.com/$REPO/releases/download/$VERSION" tarball="clipwarden-$VERSION.tar.gz"
    say "downloading clipwarden $VERSION"
    curl -fsSL -o "$TMP/$tarball" "$base/$tarball" || die "download failed: $base/$tarball"
    curl -fsSL -o "$TMP/SHA256SUMS" "$base/SHA256SUMS" || die "download failed: $base/SHA256SUMS"
    local want got
    want="$(awk -v f="$tarball" '$2 == f || $2 == "*"f { print $1 }' "$TMP/SHA256SUMS")"
    got="$(sha256sum "$TMP/$tarball" | awk '{print $1}')"
    [[ "$want" =~ ^[0-9a-f]{64}$ && "$want" == "$got" ]] || die "checksum mismatch for $tarball: refusing to install"
    say "checksum OK ($got)"
    # The checksum proves the file wasn't corrupted; the attestation proves GitHub Actions built it
    # from this repository. It needs the GitHub CLI.
    if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
      gh attestation verify "$TMP/$tarball" -R "$REPO" >/dev/null || die "build provenance check FAILED: refusing to install"
      say "build provenance verified (Sigstore)"
    elif [[ "${CLIPWARDEN_REQUIRE_PROVENANCE:-0}" == "1" ]]; then
      die "CLIPWARDEN_REQUIRE_PROVENANCE=1 but the GitHub CLI isn't installed and logged in (sudo apt install gh && gh auth login)"
    else
      warn "build provenance NOT checked (install the GitHub CLI and run 'gh auth login' to verify it next time)"
    fi
    tar -xzf "$TMP/$tarball" -C "$TMP"
    SRC="$TMP/clipwarden-$VERSION"
    [[ -d "$SRC/src/clipwarden" ]] || die "unexpected archive layout"
  fi

  # ------------------------------------------------------------ install (build aside, then one symlink switch)
  mkdir -p "$DATA/versions" "$BIN" "$(dirname "$DESKTOP")"
  chmod 700 "$DATA"
  # One installer at a time (each one cleans up old versions when it finishes).
  exec 9>"$DATA/.install.lock"
  if command -v flock >/dev/null; then
    flock -n 9 || die "another clipwarden install is running; wait for it to finish"
  fi
  local dest prev=""
  dest="$(mktemp -d "$DATA/versions/$(printf '%s' "$VERSION" | tr -c 'A-Za-z0-9._-' '_').XXXXXX")"  # unique per run
  cp -r "$SRC/src" "$SRC/examples" "$SRC/requirements-youtube.lock" "$SRC/LICENSE" "$dest/"
  cp "$SRC/scripts/install.sh" "$dest/install.sh"
  [[ -d "$SRC/sandbox" ]] && cp -r "$SRC/sandbox" "$dest/"
  printf '%s\n' "$VERSION" > "$dest/VERSION"
  say "creating a private Python environment with hash-pinned dependencies (about a minute)"
  # Built in its final folder, so the environment's own scripts (pip etc.) keep working.
  python3 -m venv "$dest/.venv"
  "$dest/.venv/bin/pip" install --quiet --disable-pip-version-check --require-hashes -r "$dest/requirements-youtube.lock" \
    || { rm -rf -- "$dest"; die "dependency install failed (nothing was changed)"; }
  # Older installs kept the app in a plain folder: file it under versions/ so the switch below works.
  if [[ -d "$APP" && ! -L "$APP" ]]; then
    mv -- "$APP" "$DATA/versions/legacy-$(date +%s)"
  fi
  [[ -L "$APP" ]] && prev="$(readlink -f -- "$APP")"
  ln -sfn "$dest" "$APP.tmp" && mv -Tf "$APP.tmp" "$APP" || die "could not switch $APP to the new version"
  # Keep the new version and the one before it: a clipwarden that is still running keeps working
  # until you restart it. Anything older goes.
  local old
  for old in "$DATA/versions"/*; do
    if [[ -d "$old" && "$old" != "$dest" && "$old" != "$prev" ]]; then
      rm -rf -- "$old"
    fi
  done

  cat > "$BIN/clipwarden" <<EOF
#!/bin/sh
$MARK
PYTHONPATH='$APP/src' exec '$APP/.venv/bin/python' -m clipwarden.cli "\$@"
EOF
  chmod 755 "$BIN/clipwarden"

  cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=clipwarden
Comment=Review, approve and publish rights-cleared clips (runs on this computer)
Exec="$BIN/clipwarden" app
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
  echo "  Help:       clipwarden --help"
  echo "  Update:     run the installer again (restart clipwarden afterwards)"
  echo "  Remove:     bash $APP/install.sh --uninstall   (your data in $DATA stays)"
}

uninstall() {
  local DATA="${XDG_DATA_HOME:-$HOME/.local/share}/clipwarden"
  local BIN="$HOME/.local/bin/clipwarden"
  local DESKTOP="${XDG_DATA_HOME:-$HOME/.local/share}/applications/clipwarden.desktop"
  [[ -n "$DATA" && "$DATA" == */clipwarden ]] || die "refusing to remove '$DATA'"
  rm -rf -- "$DATA/app" "$DATA/versions" "$DESKTOP"
  if [[ -f "$BIN" ]] && grep -q "installed by scripts/install.sh" "$BIN"; then
    rm -f -- "$BIN"
  elif [[ -e "$BIN" ]]; then
    warn "left $BIN alone (it wasn't created by this installer)"
  fi
  say "removed the app. Your data and config are still in $DATA (delete that folder to remove them too)."
}

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

main "$@"
