#!/usr/bin/env bash
# Branche « tracking » : données partagées entre les jobs (classement, solde de Joss, annonce appât).
#   tracking.sh setup            -> crée le dossier data/ (worktree de la branche tracking)
#   tracking.sh push "message" fichier...  -> commit + push des fichiers (réessaie en cas de conflit)
set -e
case "$1" in
  setup)
    git fetch --depth=1 origin +refs/heads/tracking:refs/remotes/origin/tracking 2>/dev/null || true
    if [ -d data/.git ] || [ -f data/.git ]; then exit 0; fi
    if git show-ref --verify --quiet refs/remotes/origin/tracking; then
      git worktree add -B tracking data origin/tracking
    else
      git worktree add --orphan -b tracking data
    fi
    ;;
  push)
    msg="$2"; shift 2
    cd data
    git config user.name "em-bot"
    git config user.email "em-bot@users.noreply.github.com"
    git add "$@" 2>/dev/null || true
    git diff --cached --quiet && exit 0
    git commit -q -m "$msg $(date -u '+%Y-%m-%d %H:%M') UTC"
    for i in 1 2 3; do
      git push origin HEAD:tracking && break
      git pull -q --rebase origin tracking
    done
    ;;
esac
