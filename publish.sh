#!/usr/bin/env bash
# Pushes this folder to a PUBLIC GitHub repo (created on first run) and prints
# the public raw URLs for Q13, Q14, Q1 and Q15 plus the Render deploy link.
# Usage (from WSL, inside this folder):   bash publish.sh ["commit message"]
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
REPO_NAME="${REPO_NAME:-tds-ga2}"
die() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

command -v gh >/dev/null || { sudo apt-get update -qq && sudo apt-get install -y -qq gh; } || die "install gh first"
gh auth status >/dev/null 2>&1 || gh auth login --hostname github.com --git-protocol https --web || die "gh login failed"
gh auth setup-git >/dev/null 2>&1 || true
OWNER="$(gh api user -q .login)" || die "could not read your GitHub username"

# pick up the termlog archives if they were made elsewhere
for q in q1 q15; do
  for src in "$HOME/notes/GA2/$q/submission.tar.gz" "$ROOT/../$q/submission.tar.gz"; do
    if [ -f "$src" ] && [ "$(realpath "$src")" != "$(realpath -m "$ROOT/$q/submission.tar.gz")" ]; then
      mkdir -p "$ROOT/$q" && cp -f "$src" "$ROOT/$q/submission.tar.gz" && echo "copied $src -> $q/"
      break
    fi
  done
done

[ -d .git ] || git init -q
git rev-parse --verify HEAD >/dev/null 2>&1 || git symbolic-ref HEAD refs/heads/main
git config user.email >/dev/null || git config user.email "$OWNER@users.noreply.github.com"
git config user.name >/dev/null || git config user.name "$OWNER"

# GitHub refuses workflow files unless the token has the 'workflow' scope. The
# keepalive workflow is optional, so leave it out instead of failing the push.
mkdir -p .git/info && touch .git/info/exclude
if gh auth status 2>&1 | grep -q "'workflow'"; then
  sed -i '/^\.github\/$/d' .git/info/exclude
else
  grep -qx '.github/' .git/info/exclude || echo '.github/' >>.git/info/exclude
  git rm -r -q --cached .github >/dev/null 2>&1 || true
  NO_KEEPALIVE=1
fi

git add -A
git commit -qm "${1:-TDS GA2 update}" || true
git branch -M main
if ! git remote get-url origin >/dev/null 2>&1; then
  if gh repo view "$OWNER/$REPO_NAME" >/dev/null 2>&1; then
    git remote add origin "https://github.com/$OWNER/$REPO_NAME.git"
  else
    gh repo create "$REPO_NAME" --public --source=. --remote=origin >/dev/null || die "gh repo create failed"
  fi
fi
git push -q -u origin main || die "git push failed"

RAW="https://raw.githubusercontent.com/$OWNER/$REPO_NAME/main"
check() { # path label
  local url="$RAW/$1" code i
  for ((i = 0; i < 20; i++)); do
    code=$(curl -s -o /dev/null -w '%{http_code}' "$url")
    [ "$code" = 200 ] && break
    sleep 3
  done
  printf '%-4s %s   [HTTP %s]\n' "$2" "$url" "$code"
}
printf '\n\033[1;35m================ PUBLIC URLS ================\033[0m\n'
check answers/q13.json Q13
check answers/q14.json Q14
[ -f q1/submission.tar.gz ] && check q1/submission.tar.gz Q1 || echo "Q1   (q1/submission.tar.gz not made yet)"
[ -f q15/submission.tar.gz ] && check q15/submission.tar.gz Q15 || echo "Q15  (q15/submission.tar.gz not made yet)"
printf '\033[1;35m=============================================\033[0m\n'

BLUEPRINT="https://dashboard.render.com/blueprint/new?repo=https://github.com/$OWNER/$REPO_NAME"
if [ ! -f .git/render-opened ]; then
  echo "Render one-click deploy (first time only):"
  echo "  $BLUEPRINT"
  command -v explorer.exe >/dev/null && explorer.exe "$BLUEPRINT" >/dev/null 2>&1 && echo "  (opened in your browser)"
  touch .git/render-opened
fi
if [ "${NO_KEEPALIVE:-0}" = 1 ]; then
  echo "(optional) keep Render awake 24/7: gh auth refresh -h github.com -s workflow && bash publish.sh"
fi
