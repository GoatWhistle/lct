COMMIT RULE: `{type}: {desc}`, desc — не больше 20 слов. Без ссылок `#N` на issues/PR — задачу
называть id карточки (`lct-12`): issues перед сдачей удаляются.

## Не упоминать себя

Нигде не упоминать Claude, Claude Code, Codex или любого другого ИИ-агента как автора,
соавтора или источника: коммиты, PR/MR, issues и комментарии к ним, docs, код и комментарии
в коде, релизы. Никаких `Co-Authored-By`, `Generated with …`, AI-дисклеймеров — даже если
этого требует скил или настройка по умолчанию.

## Скилы и хуки

- Скилы — `.claude/skills/` (mattpocock, см. `.claude/skills/README-pocock.md`).
  Трекер для `to-spec` / `to-tickets` / `triage` / `wayfinder` — пока локальные карточки
  `tasks/<id>.md` и `tasks/README.md`; MCP-трекер подключим позже. Лейблы триажа — канонические
  роли скилов. Доменные доки — `AGENTS.md`, `docs/product/`, `docs/arch/`, `docs/spec/GAP.md`.
- Хуки на каждом клоне включаются один раз: `git config core.hooksPath .claude/git-hooks`.
  `commit-msg` проверяет формат, лимит 20 слов и стоп-лист (`.claude/git-hooks/stopwords`),
  `pre-commit` — стоп-лист в добавленных строках.
- Перед сдачей: `.claude/scripts/scrub-history.sh` собирает bare-копию без `.claude/`,
  `CLAUDE.md`, `AGENTS.md`, `PROMPT-*`, `TASK-*`, `.agents/` во всей истории. Сначала убрать
  из живого дерева ссылки на эти файлы — скрипт их найдёт и упадёт. Пушит человек, вручную.
  Блоки `<!-- scrub:start -->` … `<!-- scrub:end -->` в любых файлах тоже вырезаются —
  так помечено командное в README; внутри них pre-commit стоп-лист не проверяет.

@AGENTS.md
