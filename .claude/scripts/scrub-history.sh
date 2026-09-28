#!/usr/bin/env bash
# Чистая копия репозитория для сдачи: из всей истории вырезаются файлы агентов,
# из сообщений коммитов — трейлеры и строки с упоминаниями агентов.
# Работает на отдельном bare-клоне, исходную репу не трогает и сам ничего не пушит.
#
#   .claude/scripts/scrub-history.sh [источник] [каталог-результата]
#   источник — путь или URL (по умолчанию корень текущей репы)
set -euo pipefail

root=$(git rev-parse --show-toplevel)
src=${1:-$root}
out=${2:-$(mktemp -d)/scrub.git}
stop=$(<"$root/.claude/git-hooks/stopwords")
files_re='CLAUDE\.md|AGENTS\.md|PROMPT-|TASK-[a-z<]|\.claude/|\.agents/'
# Внутренняя кухня работы с агентами: гайды-промпты и скилы.
trace_re='task-guide-prompt|[Сс]килл?(ы|а|ов|ам|ами|ах|у|ом|е)?\b'
# В содержимом вендоры LLM (OpenAI, Anthropic…) — законная часть продукта; ищем только агентов.
agent_re='claude|codex|copilot|cursor[- ]?(ai|agent)|co-authored-by'

[[ ! -e $out ]] || { echo "уже существует: $out" >&2; exit 1; }

echo "== клон $src -> $out"
# Только ветки и теги: refs/pull/* и прочие служебные ссылки в сдачу не идут.
git clone --quiet --bare "$src" "$out"
cd "$out"
git for-each-ref --format='%(refname)' | { grep -vE '^refs/(heads|tags)/' || true; } \
  | xargs -r -n1 git update-ref -d

# Заголовки со следами агентов заменяются явно: молча портить смысл нельзя.
# Незнакомый заголовок роняет чистку — дописать сюда замену и запустить снова.
export SCRUB_STOP="$stop" SCRUB_FILES="$files_re" SCRUB_TRACE="$trace_re"
callback=$(cat <<'PY'
import os, re
stop = re.compile(os.environ["SCRUB_STOP"], re.I)
files = re.compile(os.environ["SCRUB_FILES"])
subjects = {
    "docs: правила агентов в AGENTS.md": "docs: правила разработки",
    "chore: общие скилы, хуки коммитов, правила": "chore: правила коммитов",
    "docs: правила агентов собраны в одном файле": "docs: правила разработки в одном файле",
    "chore: скилл автономной раздачи issues исполнителям в отдельных worktree": "chore: автоматизация раздачи задач",
}
text = message.decode()
# Issues и PR в сдачу не идут: `#N` вели бы в пустоту или на чужой номер нового репо.
text = re.sub(r"^Merge pull request #\d+ from [^/\s]+/(\S+)", r"Merge branch '\1'", text)
text = re.sub(r"[ \t]*\(#\d+\)", "", text)
text = re.sub(r"(?im)^[ \t]*(close[sd]?|fix(e[sd])?|resolve[sd]?|refs?|see|related|part of)\b[^\n]*#\d+[^\n]*\n?", "", text)
subject, _, body = text.partition("\n")
subject = subjects.get(subject.strip(), subject)
if stop.search(subject) or files.search(subject) or re.search(os.environ["SCRUB_TRACE"] + "|агент", subject, re.I):
    raise SystemExit("scrub: нет замены для заголовка: " + subject)
trace = re.compile(os.environ["SCRUB_TRACE"])
def dirty(l):
    return bool(re.search(r"(^|[^\w&/])#\d+", l) or stop.search(l) or files.search(l) or trace.search(l)
                or re.match(r"\s*(co-authored-by|generated with)\b", l, re.I))
# Пункт списка со следом выкидывается вместе с продолжением, обычный абзац — целиком,
# чтобы не оставлять оборванных предложений.
def clean(para):
    blocks = []
    for l in para.split("\n"):
        if l.lstrip().startswith(("- ", "* ")) or not blocks or not blocks[-1][0]:
            blocks.append([l.lstrip().startswith(("- ", "* ")), l])
        else:
            blocks[-1].append(l)
    if any(dirty(l) for b in blocks if not b[0] for l in b[1:]):
        return None
    return "\n".join(l for b in blocks if not any(dirty(x) for x in b[1:]) for l in b[1:])
paras = [p for p in map(clean, body.strip("\n").split("\n\n")) if p and p.strip()]
body = "\n\n".join(paras)
return (subject.rstrip() + ("\n\n" + body if body else "") + "\n").encode()
PY
)

# Ссылки на вырезаемые файлы живут и в старых ревизиях документов — правятся во всех.
# Строки игнора `.claude/…` и `.agents/…` в .gitignore — тоже след.
replace=$(mktemp)
cat >"$replace" <<'RULES'
regex:(?m)^\.(claude|agents)/[^\n]*\n==>
regex:(?m)^(PROMPT|TASK)-<id>\.md[^\n]*\n==>
regex:Выполнено[^`\n]*`TASK-[^`]+`[^;\n]*;==>Выполнено;
regex:(?s)<!-- scrub:start -->.*?<!-- scrub:end -->\n\n?==>
regex: \(\[(AGENTS|CLAUDE)\.md\]\(\.\./(AGENTS|CLAUDE)\.md\)\)==>
regex: \((AGENTS|CLAUDE)\.md\)==>
RULES

echo "== переписываю историю"
uvx git-filter-repo --force --quiet --invert-paths --replace-text "$replace" \
  --path .claude/ --path CLAUDE.md --path AGENTS.md --path .agents/ \
  --path-glob '*PROMPT-*.md' --path-glob '*TASK-*.md' \
  --message-callback "$callback"

echo "== проверка"
bad=0
if git log --all --format='%an %ae %cn %ce%n%B' | grep -niE "$stop" \
   || git log --all --format='%B' | grep -nE "$files_re|$trace_re|(^|[^[:alnum:]&/])#[0-9]+"; then
  echo "!! следы в авторах или сообщениях" >&2; bad=1
fi
# shellcheck disable=SC2046 # ревизий десятки, в командную строку помещаются
if { git grep -liE "$agent_re" $(git rev-list --all) -- . ':(exclude)docs/task09-materials/**'
     git grep -lE "$files_re|$trace_re" $(git rev-list --all) -- . ':(exclude)docs/task09-materials/**'
   } | sort -u | grep .; then
  echo "!! следы в содержимом (ревизия:файл) — поправить в исходной репе коммитом и повторить" >&2
  bad=1
fi
# shellcheck disable=SC2046
if git grep -l 'scrub:start' $(git rev-list --all) -- . | grep .; then
  echo "!! незакрытый блок scrub:start" >&2; bad=1
fi
if git rev-list --all | xargs -n1 git ls-tree -r --name-only | sort -u | grep -E "$files_re"; then
  echo "!! файлы агентов в истории" >&2; bad=1
fi
(( bad == 0 )) || { echo "чистка не прошла: $out" >&2; exit 1; }

cat <<MSG
== готово: $out
Пуск только вручную и после подтверждения команды:
  git -C $out push <remote-url> --force --all
  git -C $out push <remote-url> --force --tags
GitHub хранит refs/pull/* старых PR и не даёт их переписать: если PR были —
сдавать в новый пустой репозиторий, а не в старый. Команде после пуша — переклонировать.
MSG
