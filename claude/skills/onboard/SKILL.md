---
name: onboard
description: Set up work on a project repository the sandbox has not seen before — read what the repo already answers, ask only what it cannot, and write the answers to project memory. Use on "/onboard", "set up this project", "new project", "onboard me", their Russian equivalents ("настрой проект", "новый проект", "разберись с проектом"), and whenever a task starts in a repository with no memory yet.
allowed-tools: Bash, Read, Grep, Glob, Write, AskUserQuestion
---

A project's conventions live in two places. Most are in the repository and you
read them. A few exist only in someone's head — who reviews, where tasks live,
what the team does that the code cannot show. Those are what you ask, and they
are what goes to memory.

Memory is for facts that stay true between sessions and are not derivable from
the code. A skill is for a procedure you repeat. Do not confuse them: "the
default branch is `dev`" is memory, "how to open an MR" is a skill.

## 1. Read first — never ask what the repo answers

Run these before asking anything. Every one of them removes a question.

```bash
git -C <repo> remote -v                      # host: gitlab / github / other
git -C <repo> symbolic-ref refs/remotes/origin/HEAD   # default branch
git -C <repo> log --oneline -20              # commit style, task keys in messages
git -C <repo> log --format='%an' -100 | sort | uniq -c | sort -rn | head   # who works here
git -C <repo> branch -r --sort=-committerdate | head  # branch naming
ls -a <repo>                                 # CLAUDE.md, CONTRIBUTING, Makefile, CI config
```

Then read what you found: `CLAUDE.md`, `README`, `CONTRIBUTING`, the CI config
(`.gitlab-ci.yml`, `.github/workflows/`), and the test/lint commands in
`Makefile`, `package.json` or `pyproject.toml`.

Task keys in commit messages give you the tracker's prefix for free — a subject
line starting `ABC-123` means the project's tracker issues `ABC-` keys.

## 2. Ask what is left — at most 6 questions, in one batch

Ask only what reading did not answer. Use `AskUserQuestion`, batch them, and
offer the value you inferred as the first option so agreeing is one click.

The questions worth asking, because a repository almost never holds them:

| Question | Why the repo cannot answer it |
| --- | --- |
| Who reviews your MRs, and is approval required to merge? | Reviewer assignment lives in the tracker or in habit, not in the code |
| Where do tasks live, and what statuses do they move through? | The board's columns are not in the repo; the transition names matter for automation |
| Which branch do you branch off, and which one do you merge into? | `origin/HEAD` gives a default, not the team's rule — those differ when a release branch exists |
| What must pass before you open an MR — tests, lint, types, a migration check? | CI shows what runs after; it does not show what the team expects before |
| Where does the service run, and how do you see its logs? | Environment URLs and log tooling are outside the repository |
| Anything the code would mislead me about? | Dead directories, a config that looks active and is not, a test suite nobody runs |

Skip a row the moment reading answered it. Six is the ceiling, not the target —
three well-chosen questions beat six with two obvious ones.

**Do not ask what a skill would cover.** "How do I deploy" is a procedure; if
the answer is long, that is a signal to offer a skill in step 4, not to write
six memory files about it.

## 3. Write memory — one fact per file

Memory lives per project, keyed by the working directory's path:

```bash
ls -d /root/.claude/projects/$(pwd | sed 's|/|-|g')/memory
```

Create it if it is missing. `end` deliberately never prunes it, so what you write
there is permanent — which is the reason to write facts and not guesses.

Front matter and body follow the format already in use:

```markdown
---
name: <short-kebab-case-slug>
description: <one line, used to decide relevance later>
metadata:
  type: project
---

<the fact, and what follows from it. Link related memories with [[their-name]].>
```

Then add one line per file to `MEMORY.md` in that same directory:
`- [Title](file.md) — hook`. That index is what loads into every session.

Rules that keep memory useful:

- **One fact per file.** "Reviewer is X" and "default branch is dev" are two files.
- **Convert relative dates.** "last sprint" is worthless in three months; write the date.
- **Do not save what the repo records.** Directory layout, the test command that
  is already in the `Makefile`, anything in `CLAUDE.md` — those are read, not remembered.
- **Write the consequence, not just the fact.** "Merges into `dev`, never `main`
  — `main` is release-only and a direct MR there gets closed" beats "default branch: dev".

## 4. Offer a skill only where a procedure repeats

After writing memory, look at what the answers describe. A multi-step sequence
the user will repeat — open an MR, move a ticket to QA, deploy to a stand, pull
logs — is a skill, and the sandbox may already have one:

```bash
ls /opt/skills/20-project /opt/skills/10-base
```

Name what is missing, one line each, and let the user pick. Do not write skills
unasked: a skill nobody invokes is worse than no skill, because it still gets
loaded and read.

## 5. Report

Say what you read, what you wrote, and what you did not ask. Format:

```
Read: <files and commands that answered questions>
Memory: <file> — <fact>          (one line each)
Skills worth adding: <name> — <what it would do>   (omit if none)
```

Last line — the action: what to do next, or the first task to start on.
