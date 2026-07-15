#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p ~/.local/share/icons ~/.local/share/applications
cp "$SCRIPT_DIR/assets/subtranslg.png" ~/.local/share/icons/llm-subtrans.png
cat > ~/.local/share/applications/llm-subtrans.desktop <<EOF
[Desktop Entry]
Name=LLM Subtrans
Comment=Translate subtitles using LLM
Exec=bash -c 'cd $SCRIPT_DIR && ./gui-subtrans.sh'
Icon=\$HOME/.local/share/icons/llm-subtrans.png
Type=Application
Terminal=false
Categories=Utility;AudioVideo;
EOF
update-desktop-database ~/.local/share/applications/
echo "Desktop entry installed successfully."
