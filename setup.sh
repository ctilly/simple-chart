#!/bin/sh

set -u

cd "$(dirname "$0")" || exit 1

case "$(uname -s 2>/dev/null || printf unknown)" in
    MINGW*|MSYS*|CYGWIN*)
        echo ""
        echo "  =============================================================="
        echo "   This installer is for macOS and Linux."
        echo "   On Windows, please double-click setup.bat instead."
        echo "  =============================================================="
        echo ""
        exit 1
        ;;
esac

UV_EXE=""

status() {
    printf "%s\n" "  [i] $1"
}

ok() {
    printf "%s\n" "  [OK] $1"
}

fail() {
    printf "%s\n" ""
    printf "%s\n" "  [X] ERROR: $1"
}

find_uv() {
    if command -v uv >/dev/null 2>&1; then
        UV_EXE="uv"
        return 0
    fi

    for candidate in \
        "$HOME/.local/bin/uv" \
        "$HOME/.cargo/bin/uv"
    do
        if [ -x "$candidate" ]; then
            UV_EXE="$candidate"
            return 0
        fi
    done

    return 1
}

install_uv() {
    tmp_file="$(mktemp "${TMPDIR:-/tmp}/simplechart-uv-install.XXXXXX")" || return 1
    trap 'rm -f "$tmp_file"' EXIT HUP INT TERM

    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh -o "$tmp_file" || return 1
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$tmp_file" https://astral.sh/uv/install.sh || return 1
    else
        fail "Neither curl nor wget is available."
        echo ""
        echo "      Install curl or wget, then run this script again."
        echo "      uv installation docs:"
        echo "        https://docs.astral.sh/uv/getting-started/installation/"
        return 1
    fi

    sh "$tmp_file"
}

echo ""
echo "  =============================================================="
echo "                     Simple Chart Setup"
echo "  =============================================================="
echo ""
status "This will install everything needed to run Simple Chart."
status "It may take a few minutes on the first run."
echo ""

status "Step 1 of 5: Checking for 'uv' package manager..."

if find_uv; then
    ok "Found uv."
else
    echo ""
    status "'uv' not found. Installing it with Astral's official installer..."
    echo ""
    echo "  --- begin output from Astral's uv installer --------------------"
    echo ""

    if ! install_uv; then
        fail "The uv installer failed."
        echo ""
        echo "      Possible causes:"
        echo "        - No internet connection"
        echo "        - A firewall is blocking the download"
        echo "        - astral.sh is unreachable from your network"
        echo ""
        echo "      Manual installation docs:"
        echo "        https://docs.astral.sh/uv/getting-started/installation/"
        exit 1
    fi

    echo ""
    echo "  --- end output from Astral's uv installer ----------------------"
    echo ""
    status "The installer's success message only means uv is installed."
    status "Python and Simple Chart are installed in the next steps."
    echo ""
fi

status "Step 2 of 5: Locating uv..."

if ! find_uv; then
    fail "uv was installed, but this shell cannot find it yet."
    echo ""
    echo "      Try closing this terminal, opening a new one, and running:"
    echo "        ./setup.sh"
    echo ""
    echo "      If that still fails, check whether uv exists at:"
    echo "        $HOME/.local/bin/uv"
    exit 1
fi

ok "Using uv at: $UV_EXE"
echo ""

status "Step 3 of 5: Setting up Python 3.13..."
echo "      If Python 3.13 is not already installed, uv will download it."
echo ""
echo "  --- begin uv venv output ---------------------------------------"
echo ""

if ! "$UV_EXE" venv --python 3.13; then
    echo ""
    echo "  --- end uv venv output -----------------------------------------"
    fail "Failed to create the virtual environment."
    echo ""
    echo "      This may indicate a network issue while downloading Python,"
    echo "      or a permissions problem in this folder."
    exit 1
fi

echo ""
echo "  --- end uv venv output -----------------------------------------"
ok "Python 3.13 is ready."
echo ""

status "Step 4 of 5: Installing Simple Chart and dependencies..."
echo "      This may take a few minutes the first time."
echo ""
echo "  --- begin uv pip install output --------------------------------"
echo ""

if ! "$UV_EXE" pip install -e .; then
    echo ""
    echo "  --- end uv pip install output ----------------------------------"
    fail "Failed to install Simple Chart dependencies."
    echo ""
    echo "      Common causes:"
    echo "        - No internet connection during package download"
    echo "        - A missing or malformed pyproject.toml in this folder"
    echo "        - A dependency conflict"
    echo ""
    echo "      Review the messages above for details."
    exit 1
fi

echo ""
echo "  --- end uv pip install output ----------------------------------"
ok "Simple Chart and all dependencies installed."
echo ""

status "Step 5 of 5: Setup complete."
echo ""
echo "  =============================================================="
echo "   [OK] Setup complete! Everything is now installed."
echo "  =============================================================="
echo ""
echo "   To launch Simple Chart:"
echo ""
echo "     . .venv/bin/activate"
echo "     simplechart"
echo ""
