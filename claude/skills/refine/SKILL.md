---
name: refine
description: Разбор задачи перед выполнением — стоит ли её делать и что именно делать. Вердикт плюс WHAT/WHERE/HOW/DONE WHEN и уточняющие вопросы. Используй при "/refine", "уточни задачу", "разбери задачу", "составь план", а также когда постановка расплывчата и есть риск сделать не то.
---

You are refining a task before execution. Two questions, in order: is this
worth doing, and what exactly is the work. Output: the verdict, the rewritten
task and the few questions the repository could not answer. Nothing else.

## 1. Gather context first — read, don't ask

Answer your own questions before writing anything.

**Read as deep as the task is wide.** Match the depth to what the change can
break, not to the list below:

| Task | Read | Output |
|---|---|---|
| One obvious file, the change alters no observable behaviour — a typo, a comment, a log line | State plus that file. Stop there | Short form |
| Everything else | The full list below | Full form |

The short path skips step 2 — nothing is in dispute, so there is no verdict 
to ground. The narrow read is a bet that the task is what it looks like. 
The moment it turns out otherwise — the caller matters, the ticket disagrees, 
the tree is dirty — you are on the full path: go read the rest. Nothing below 
is optional once you are there.

- State: `git status` and the diff. Never assume a clean tree. Existing
  changes are the user's work — report them, never fold them into scope.
- Ticket: if the task has a key or link, read it — comments and whatever it
  links to included. Acceptance criteria usually resolve much of the
  ambiguity. A ticket already in a testing or closed state is a finding on
  its own — say so and stop. Write down the **expected behaviour**: what
  the author considers correct, and under which scenario. If the ticket
  never states it ("it works wrong"), that is your first finding — ask the
  author.
- Code: find the entry point with grep / the code index, trace the flow end
  to end — callers, siblings sharing the same helper, tests already covering
  it. The implementation is often not in the file the ticket names. Go all the
  way to where the observed behaviour is actually produced: entry point →
  logic → storage → query/schema.
- Contracts: whatever someone else depends on — API, schema, event, config,
  CLI, migration. Changing one means checking both producer and consumer.
- History: `git log --grep=<KEY>` and `git log -S` on the touched files.
  A previous attempt or revert changes the plan.
- Rules: CLAUDE.md, directory-level instructions, skills relevant to the layer.

A question grep could answer is homework you skipped, not a question.
A guess about what a query returns is not a substitute for running it on real
data.

## 2. Verdict — is there work here at all

Compare the expected behaviour against what the code actually does. Ground the
verdict in code — the line that produces the behaviour and why it is or isn't
correct. A retelling of the ticket is not a verdict; without the line, don't
issue one.

| What you found | Verdict | What follows |
|---|---|---|
| The code really is wrong | **bug** | Go to step 3 |
| The code is right, the ticket's expectation is wrong | **bad premise** | Report it, wait for a decision. Don't touch the code |
| The described behaviour does not reproduce | **no repro** | Ask the author for steps |
| Not this component's task | **wrong component** | Say so, suggest reassigning |
| Several claims of different kinds | **mixed** | Split them, one verdict each. Only the "bug" items become work |
| Nothing was reported broken — a change, a feature, a chore | **task** | Go to step 3 |

**"Mixed" is normal for QA cards.** A three-item ticket regularly holds one
real bug, one deliberate behaviour and one feature request. Don't force a
single verdict: half the work would otherwise be lost or silently pulled into
scope. Items ruled "bad premise" or "feature" belong in HOW as what is **not**
being done — otherwise QA sends the task back after checking it against the
expected result.

If the discrepancy stays unclear, say so instead of filling the gap: correct
logic behind a reported "bug" is as common as the reverse.

Verdict other than "bug", "mixed" or "task" — there is no work to plan.
Report the verdict and stop; the output ends there.

## 3. Rewrite the task

- WHAT: the observable change, one sentence. If reading revealed the root
  cause behind the reported symptom, name the root cause.
- WHERE: exact files/functions found in step 1, plus every sibling caller
  on the same path.
- HOW: constraints that bind here — project rules, existing patterns,
  compatibility, skills to use, what must NOT change. Incidental refactoring
  is not in this scope.
- DONE WHEN: the test/command/gate that goes green, or the observable
  behaviour reproduced. Never "works correctly".
- FOUND: 1–3 lines on what reading changed versus the original wording
  (root cause, wider scope, already implemented, previous revert, blocked,
  unrelated changes in the tree). Omit if nothing.

## 4. Ask only what changes the work

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

## 5. Output format

Short form — the whole answer, nothing else:

```
WHAT: <one sentence>
DONE WHEN: <the command or observable behaviour>
```

No verdict, no WHERE, no HOW, no FOUND, no questions. A small task that needs
any of them was not a small task — switch to the full form.

Full form:

```
VERDICT: <bug | bad premise | no repro | wrong component | mixed | task>
  <the line of code that grounds it>

WHAT:      <one sentence>
WHERE:     <file:function, one per line>
HOW:       <constraint, one per line — including what must NOT change>
DONE WHEN: <the test/command/gate, or the behaviour reproduced>
FOUND:     <1–3 lines; omit the section if nothing>
PLAN:      <step → files → check, one per line; only for a risky task>

QUESTIONS (0–3):
  1. <X or Y? Default: X, because … Under X I will …>
```

A verdict other than "bug", "mixed" or "task" ends the output at VERDICT — the
rest is a plan for work that is not happening.

## 6. Risky task

Migration, public or event contract, architectural choice, backward
compatibility, security-sensitive behaviour: add an ordered plan
(step → files → check) in PLAN, then stop for confirmation. File count alone
is not a reason to stop.

## Rules

- Do not implement, do not edit files, do not touch git state.
- Do not invent missing requirements.
- Answer in the user's language.
- Touch the tracker only on confirmation: a comment notifies the author.
- If context gathering is blocked (no ticket access, empty submodule), say
  that in the first line — it is the main finding.
