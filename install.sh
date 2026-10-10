#!/bin/sh
# heiika のセットアップ: Python の環境を作り、skill を ~/.claude/skills に置く
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
python3 -m venv "$HERE/.venv"
"$HERE/.venv/bin/pip" install -r "$HERE/requirements.txt"
mkdir -p ~/.claude/skills
for d in "$HERE"/skills/*/; do
  name=$(basename "$d")
  rm -rf ~/.claude/skills/"$name"
  ln -s "$d" ~/.claude/skills/"$name"
done
echo "HEIIKA=$HERE を環境変数に設定してください（例: export HEIIKA=$HERE を ~/.zshrc に）"
