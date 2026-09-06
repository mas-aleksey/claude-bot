---
name: refine
description: Разбор задачи перед выполнением — стоит ли её делать и что именно делать. Вердикт плюс WHAT/WHERE/HOW/DONE WHEN и уточняющие вопросы. Используй при "/refine", "уточни задачу", "разбери задачу", "составь план", а также когда постановка расплывчата и есть риск сделать не то.
---

You are helping refine a task before execution.

Analyze the task in $ARGUMENTS and do the following:

## 0. Check the available context first

Answer your own questions from the sources and tools you already have, not
from the user. A question you could have answered by looking is homework you  
skipped, not a question. Ground what you write in evidence, not in a retelling
of the request.

Inspection covers read-only work: analysis, searches, calculations, queries,
previews, checks. It must not change the subject of the task or any external
state.

## 1. Rewrite the task

- WHAT: the specific action — the verified cause if you found one, not the symptom
- WHERE: the exact scope — objects, sources, systems, documents, or data involved
- HOW: constraints, patterns, skills to use, what must NOT change
- DONE WHEN: the check that proves it, never "works correctly"

Take reasonable defaults where the choice does not materially change the
outcome, and state each one. Do not invent facts, preferences, permissions or
constraints.

Adjacent problems you discover do not enter the scope. Mention one only if it
blocks the task or creates a material risk.

## 2. Ask only what changes the work

Each question as a fork with a default: "X or Y? Default: X, because … Under X
I will …". Cap: 3. Zero is valid — then list the defaults you took.

Never ask the generic checklist ("is the scope clear?", "how thorough should I
be?") — resolve those from the existing state and record the answer in HOW.

## 3. Stop if there is no work

Already behaves as intended, does not reproduce, already done, or the cause
sits somewhere you were not asked about: say that with the evidence and stop.
Do not plan work that is not happening.

## 4. Stop for confirmation if the task is risky

Hard to undo, other things depend on it, or it can lose data: show an
execution plan plus how to roll back, then wait.

## Rules

- Do not start implementing.
- Output only the refinement: the rewritten task, the defaults, the questions,
  and where they apply the no-work verdict or the risk plan with its rollback.
