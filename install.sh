#!/usr/bin/env sh
# Install `it` and `itui` for the current user (Linux / macOS).
#   ./install.sh            # venv in ./.venv, launchers in ~/.local/bin
#   BIN_DIR=/some/dir ./install.sh
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
BIN_DIR=${BIN_DIR:-"$HOME/.local/bin"}

if command -v uv >/dev/null 2>&1; then
  uv venv -q --allow-existing "$HERE/.venv"
  uv pip install -q --python "$HERE/.venv/bin/python" -r "$HERE/requirements.txt"
else
  python3 -m venv "$HERE/.venv"
  "$HERE/.venv/bin/pip" install -q -r "$HERE/requirements.txt"
fi

mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/it" <<EOF
#!/usr/bin/env sh
exec "$HERE/.venv/bin/python" "$HERE/it.py" "\$@"
EOF
cat > "$BIN_DIR/itui" <<EOF
#!/usr/bin/env sh
exec "$HERE/.venv/bin/python" "$HERE/itui.py" "\$@"
EOF
chmod +x "$BIN_DIR/it" "$BIN_DIR/itui"

echo "installed: $BIN_DIR/it, $BIN_DIR/itui"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "note: $BIN_DIR is not on your PATH — add it in your shell profile" ;;
esac
if [ ! -f "${INITTASKS_HOME:-$HOME/.config/inittasks}/config.json" ] && [ -z "${INITTASKS_TOKEN:-}" ]; then
  # first install: connect an account now if there's someone at the keyboard
  if [ -t 0 ] && [ -t 1 ]; then
    "$BIN_DIR/it" login || echo "next: it login        # paste your itk_… key"
  else
    echo "next: it login        # paste your itk_… key"
  fi
fi
