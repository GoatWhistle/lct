#!/usr/bin/env bash
# Один исполнитель claude -p на issue в своём worktree.
# run-worker.sh [--dry-run] <N> <slug> [строка части]
# Повторный запуск с тем же slug продолжает работу в существующем worktree и дописывает лог.
set -euo pipefail

dry=0
if [[ ${1:-} == --dry-run ]]; then dry=1; shift; fi
if [[ $# -lt 2 ]]; then
  echo "usage: run-worker.sh [--dry-run] <N> <slug> [строка части]" >&2
  exit 2
fi
n=$1 slug=$2 part=${3:-}

# Корень основного клона, даже если скрипт вызван из worktree.
root=$(cd "$(git rev-parse --git-common-dir)/.." && pwd)
wt=$root/.claude/worktrees/$slug
log=$root/.claude/worktrees/logs/$slug.log

prompt="Ты работаешь автономно в своём worktree, ответов не будет. Вопрос комментарием в issue с вариантами,
  бери рекомендованный и продолжай. После работы открывай и мердж PR /implement $n"
if [[ -n $part ]]; then prompt+=$'\n'"$part"; fi

run() {
  if (( dry )); then printf '+ %q' "$1"; shift; printf ' %q' "$@"; echo; else "$@"; fi
}

run git -C "$root" fetch -q origin
if [[ -d $wt ]]; then
  echo "worktree $wt уже есть — продолжаем в нём"
elif git -C "$root" show-ref -q --verify "refs/heads/feat/$slug"; then
  run git -C "$root" worktree add -q "$wt" "feat/$slug"
else
  run git -C "$root" worktree add -q -b "feat/$slug" "$wt" origin/main
fi
run mkdir -p "$(dirname "$log")"

if (( dry )); then
  echo "+ cd $wt && claude -p <prompt> --model ${MODEL:-opus} --permission-mode bypassPermissions >> $log 2>&1"
  echo "--- prompt"
  echo "$prompt"
  exit 0
fi

printf '\n===== %s запуск #%s\n' "$(date -u +%FT%TZ)" "$n" >> "$log"
(cd "$wt" && claude -p "$prompt" --model "${MODEL:-opus}" --permission-mode bypassPermissions) >> "$log" 2>&1 || true
echo "#$n $slug: исполнитель завершился, лог $log"
