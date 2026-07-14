#!/bin/bash
# Sync local skill → GitHub repo
# Usage: ./sync.sh "commit message"

set -e
REPO_DIR="/tmp/feishu-wiki-paste"
SKILL_DIR="~/.openclaw/workspace/agents/feishu-wiki-paste/skills/feishu-wiki-paste"
TOKEN_FILE="~/.openclaw/workspace/agents/feishu-wiki-paste/.github_token"

MSG="${1:-Update skill files}"

# Copy latest from skill dir, stripping secrets for public repo
cp "$SKILL_DIR/paste_utils.py" "$REPO_DIR/paste_utils.py"
sed -E 's/APP_ID_PLACEHOLDER/APP_ID_PLACEHOLDER/g; s/APP_SECRET_PLACEHOLDER/APP_SECRET_PLACEHOLDER/g' \
  "$SKILL_DIR/SKILL.md" > "$REPO_DIR/SKILL.md"

cd "$REPO_DIR"
git add -A
git diff --staged --quiet || git commit -m "$MSG"

TOKEN=$(cat "$TOKEN_FILE")
git push "https://kittleyu:${TOKEN}@github.com/kittleyu/feishu-wiki-paste.git" main
echo "✅ Synced to GitHub"
