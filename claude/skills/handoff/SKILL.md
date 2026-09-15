---
name: handoff
description: Write down the state of an unfinished task so the next session can pick it up with a small context instead of a long one. Use on "/handoff", "hand this over", "save the state", "write up where we are", "I'm ending the session", their Russian equivalents ("передай состояние", "запиши где мы", "сохрани контекст", "заканчиваю сессию"), and whenever the context is large and the task is not finished.
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

You are writing a handoff note so this task can continue in a new session.

The point is not a summary. The point is that the next session reaches the same
understanding from a small file instead of from a hundred thousand tokens of
conversation. Everything it can get from the repository itself — code, git log,
`CLAUDE.md` — does not belong here. Only what would be lost when this session ends.

## 0. Where the file goes

```
/root/.claude/handoff/<project>--<task>.md
```

`<project>` is the working directory's basename, `<task>` a two or three word slug
of the task. The directory persists between container restarts, and the repository
stays clean. It already exists or `mkdir -p` makes it.

The file exists already — rewrite it in place. This is the current state, not a
log: stale steps mislead the next session more than an empty file would.

## 1. What to write

```markdown
# <task in one line>

**Проект:** <path> · **Ветка:** <branch> · **Обновлено:** <YYYY-MM-DD>

## Цель
Что должно стать правдой, когда задача закончена. Проверяемо, без «работает корректно».

## Сделано
- <что именно, с путями: `src/webui.py:412`>

## Осталось
- <следующий шаг первым, он самый важный>

## Решения
- <что выбрано> — потому что <причина>. Отменять только если <при каком условии>.

## Тупики
- <что пробовали и почему не вышло — чтобы следующая сессия не прошла этот круг заново>

## Где смотреть
- `путь:строка` — <зачем он нужен>

## Первый шаг новой сессии
<одна команда или один промпт>
```

Sections with nothing in them go away. An empty `## Тупики` is honest and costs
nothing to remove.

## 2. Rules

- **Facts, not narration.** «Поправил парсер» is worthless. «`items()` читает с
  байтового оффсета, `src/webui.py:130`» is the thing.
- **Every path with a line number** where a line number exists. The next session
  opens the exact spot instead of reading the whole file.
- **Every decision with its reason.** A decision without a reason gets re-litigated
  in the next session, and that costs more than writing it down.
- **Dead ends are the most valuable part.** They are the only thing here that cannot
  be recovered from the code. Without them the next session repeats the same attempts.
- **No retelling of the code.** If it can be read with `grep`, it goes as a path,
  not as a quote.
- **Checks, not feelings.** «Тесты зелёные» needs the command that proves it.
- Keep it under a hundred lines. A handoff that needs scrolling has become the
  context it was meant to replace.

## 3. What not to write

- A history of the conversation. Who asked what, in which order, is dead weight.
- Anything already in `CLAUDE.md` or the commit messages.
- Plans for work nobody asked for. Unfinished scope only.
- Secrets, tokens, passwords. They live in `.env`, and the note is a plain file.

## 4. Finish

Print two lines, nothing else:

```
<путь к файлу>
Новая сессия, первый промпт: прочитай <путь> и продолжи
```

Do not offer to continue working in this session. The point of the handoff is
that this session ends.
