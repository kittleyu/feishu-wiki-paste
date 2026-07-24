#!/bin/bash
# 将本地 skill 同步到 GitHub 仓库（安全、无密钥版本）
# 用法：./sync.sh "提交说明"
#
# ⚠️ 安全约定（分享前必读）：
# - 本仓库【绝不】包含任何真实凭证（飞书 App ID/Secret、GitHub token 等）。
# - 飞书凭证只通过仓库根目录的 .env 提供（已被 .gitignore 排除），
#   SKILL.md / README.md / .env.example 中一律使用占位符，请勿填入真值。
# - 推送使用 git remote 中已配置的地址（推荐 SSH + deploy key），
#   本脚本不内嵌任何 token、不引用任何个人绝对路径。
# - 提交前会自动扫描疑似真实凭证，发现即中止，避免误提交。

set -e

MSG="${1:-Update skill files}"

# 切到本脚本所在目录（即 skill 仓库根）
cd "$(dirname "$0")"

# 🔒 安全闸门：提交前检测是否误含真实飞书凭证
#   占位符（cli_xxxxxxxx / *_PLACEHOLDER）不会命中，只有真实值会触发中止。
if grep -rInE "cli_[A-Za-z2-7]{8,}|app_secret[=:][[:space:]]*['\"]?[A-Za-z0-9]{16,}" \
    --include="*.py" --include="*.md" --include="*.sh" --include="*.example" . 2>/dev/null \
    | grep -v "xxxxxxxx" | grep -v "PLACEHOLDER"; then
  echo "❌ 检测到疑似真实凭证，已终止提交。" >&2
  echo "   请确认：.env 未被提交、SKILL.md/README.md 仅使用占位符。" >&2
  exit 1
fi

git add -A
git diff --staged --quiet || git commit -m "$MSG"

git push

echo "✅ Synced to GitHub"
