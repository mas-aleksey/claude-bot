---
name: refine
description: Переформулировать задачу перед выполнением — WHAT/WHERE/HOW/DONE WHEN плюс уточняющие вопросы. Используй при "/refine", "уточни задачу", "разбери задачу", "составь план", а также когда постановка расплывчата и есть риск сделать не то.
---

You are refining a task before execution. Output: the rewritten task and the
few questions the repository could not answer. Nothing else.

## 1. Gather context first — read, don't ask

Answer your own questions before writing anything.

- State: `git status` and the diff. Never assume a clean tree. Existing
  changes are the user's work — report them, never fold them into scope.
- Ticket: if the task has a key or link, read it. Acceptance criteria and
  comments usually resolve much of the ambiguity.
- Code: find the entry point with grep / the code index, trace the flow end
  to end — callers, siblings sharing the same helper, tests already covering
  it. The implementation is often not in the file the ticket names.
- Contracts: whatever someone else depends on — API, schema, event, config,
  CLI, migration. Changing one means checking both producer and consumer.
- History: `git log --grep=<KEY>` and `git log -S` on the touched files.
  A previous attempt or revert changes the plan.
- Rules: CLAUDE.md, directory-level instructions, skills relevant to the layer.

A question grep could answer is homework you skipped, not a question.

## 2. Rewrite the task

- WHAT: the observable change, one sentence. If reading revealed the root
  cause behind the reported symptom, name the root cause.
- WHERE: exact files/functions found in step 1, plus every sibling caller
  on the same path.
- HOW: constraints that bind here — project rules, existing patterns,
  compatibility, skills to use, what must NOT change.
- DONE WHEN: the test/command/gate that goes green, or the observable
  behaviour reproduced. Never "works correctly".
- FOUND: 1–3 lines on what reading changed versus the original wording
  (root cause, wider scope, already implemented, previous revert, blocked,
  unrelated changes in the tree). Omit if nothing.

## 3. Ask only what changes the work

A question qualifies only if all three hold:
1. Ticket, code, docs and history do not answer it.
2. Different answers produce materially different diffs.
3. Guessing wrong costs more than asking (data loss, public behaviour,
   compatibility, security, a lost day).

Format each one as a fork with a default:
"X or Y? Default: X, because … Under X I will …"

Hard cap: 3. Zero is valid — say so and list the defaults you took.
Never ask the generic checklist ("are tests required?", "is the scope
clear?"). Resolve those from repository conventions and record the answer
in HOW / DONE WHEN.

## 4. Risky task

Migration, public or event contract, architectural choice, backward
compatibility, security-sensitive behaviour: add an ordered plan
(step → files → check) and stop for confirmation. File count alone is not a
reason to stop.

## Rules

- Do not implement, do not edit files, do not touch git state.
- Do not invent missing requirements.
- Answer in the user's language.
- If context gathering is blocked (no ticket access, empty submodule), say
  that in the first line — it is the main finding.
