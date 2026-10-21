# zephyr-reviewer

Source intelligence for the Zephyr tree, exposed over [MCP](https://modelcontextprotocol.io), and
an independent review agent that drives it through [OpenRouter](https://openrouter.ai).

A diff-only reviewer guesses. This one looks things up. The model reviewing a pull request can read
whole functions at the head commit, find a symbol's definition and callers, read the Kconfig block a
`select` points at, open the devicetree binding a driver claims to implement, see which twister
suites cover the area, and find out who maintains it. It then submits a structured review that is
validated against the diff and posted back to the pull request.

```
GitHub Actions runner (one job per pull request)
┌───────────────────────────────────────────────────────────────────────────┐
│  zephyr-reviewer review                                                   │
│  ┌──────────────────────┐   OpenAI-style tool calls   ┌─────────────────┐ │
│  │  review agent        │ ◀──────────────────────────▶│   OpenRouter    │ │
│  │  (orchestrator)      │                             │ model routing   │ │
│  └──────────┬───────────┘                             └─────────────────┘ │
│             │ MCP over stdio                                              │
│  ┌──────────▼───────────┐   git plumbing, pinned to the head commit       │
│  │  zephyr-source-intel │ ─────────────────────────▶  Zephyr checkout     │
│  │  MCP server          │   nothing in the tree is executed               │
│  └──────────────────────┘                                                 │
│             │ findings ─▶ GitHub PR comment / inline review / artifacts   │
└───────────────────────────────────────────────────────────────────────────┘
```

Nothing is deployed. Each workflow run starts its own MCP server, uses it to gather context,
reviews, publishes, and exits. The MCP tool surface is the contract; when indexing, review history
and project knowledge move into a shared service, the agent and the workflow stay the same and the
server behind the tools changes.

## Status

Phase 1, working end to end against a local checkout and scripted services; not yet run against a
live pull request with a paid model. See [What is not done](#what-is-not-done).

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
```

Explore the tool surface against a real Zephyr checkout:

```bash
cd ~/zephyrproject/zephyr
BASE=$(git merge-base HEAD upstream/main)

zephyr-reviewer tools --base "$BASE"
zephyr-reviewer call  --base "$BASE" find_definition name=k_sem_take
zephyr-reviewer call  --base "$BASE" kconfig_symbol  name=CONFIG_I2C_RTIO
zephyr-reviewer call  --base "$BASE" dt_binding      compatible_or_name=nordic,nrf-twim
zephyr-reviewer call  --base "$BASE" maintainers     path=drivers/i2c/i2c_nrfx_twim.c
```

Review a local branch without touching GitHub:

```bash
export OPENROUTER_API_KEY=sk-or-...
zephyr-reviewer review --repo ~/zephyrproject/zephyr --base "$BASE" --head HEAD --dry-run
```

Review a real pull request, posting nothing (artifacts land in `review-output/`):

```bash
export GITHUB_TOKEN=$(gh auth token)
zephyr-reviewer review --repo ~/zephyrproject/zephyr --repository zephyrproject-rtos/zephyr --pr 12345 --dry-run
```

Drop `--dry-run` to post. Anchored findings go inline by default; `--no-inline` puts everything in
the top-level comment. `--ask` shows the finished review and prompts before anything is posted, with an
option to edit `comment.md` first:

```
Post the summary comment and 2 inline comment(s) to PR #12345? [y]es / [n]o / [e]dit comment.md:
```

### Publish modes

`publish` is `post`, `ask` or `never` (`dry_run: "true"` means `never`). In a job with no terminal,
`ask` saves the review without posting and prints the command to publish it after a person has
read it:

```bash
zephyr-reviewer post --output-dir review-output --repository zephyrproject-rtos/zephyr [--inline]
```

`post` reads `comment.md` and `inline_comments.json` from the directory, so both can be edited or
trimmed first, asks for confirmation (or `--yes`), and publishes with the same hidden marker so a
later run updates it in place. This is the shape Zephyr's AI policy asks for: a human reads the
model's output before it reaches the pull request. Like the other safety settings, `publish` can
be tightened by flags but not relaxed below the workflow's value.

### Use the MCP server from another client

The server is a normal MCP server. Point Claude Code, an IDE, or any MCP client at it to get the
same lookups while reviewing by hand:

```bash
zephyr-reviewer serve --repo ~/zephyrproject/zephyr --base upstream/main --head HEAD
# or as a long-lived HTTP endpoint, the shape the shared service will take:
zephyr-reviewer serve --repo ~/zephyrproject/zephyr --base upstream/main --transport streamable-http --port 8765
```

## The tools

| Tool | Answers |
| --- | --- |
| `pr_overview` | Commits, changed files with +/- counts, MAINTAINERS area per file |
| `commit_messages` | Full commit messages, oldest first |
| `file_diff` | Diff of one changed file with chosen context |
| `read_file` / `read_file_at_base` | Numbered source at head (added lines marked `+`) or at base |
| `search` / `list_files` | `git grep` (POSIX ERE) and glob listing over the head tree |
| `find_definition` | Where a C function, macro, struct, typedef, Kconfig symbol, DT compatible or CMake function is defined; resolves `k_foo()` to `z_impl_k_foo()` |
| `find_references` | Callers and users, counted per area, changed files first |
| `kconfig_symbol` | Every definition block of a symbol and its users in Kconfig, `.conf`, CMake and source |
| `dt_binding` | Binding YAML for a compatible, the drivers that bind to it, the nodes that use it |
| `maintainers` | MAINTAINERS.yml areas, with get_maintainer.py's matching rules, read from the trusted base commit |
| `related_tests` | twister suites (tests.yaml / sample.yaml) covering a path or mentioning a symbol |
| `test_candidates` / `list_platforms` | Suites, scenarios, tags and platforms implicated by the change; valid platform identifiers |
| `file_history` / `blame` | Recent commits and blame for a range |
| `project_guidelines` | AGENTS.md and the doc/contribute pages by topic |

Every result is text, bounded in size, and says when it was truncated. Every query runs against an
explicit commit through `git show`, `git grep` and `git ls-tree`, never the working tree, and
nothing from the tree is executed. That is what makes it safe to run on a fork's head under
`pull_request_target`.

## The review loop

1. The orchestrator resolves the pull request (head SHA, merge base via the compare API), fetches
   both commits by SHA if the checkout lacks them, and starts the MCP server over stdio.
2. The system prompt is the Zephyr review profile (`src/zephyr_reviewer/review/profiles/zephyr.md`)
   plus working rules. The opening message carries the PR description, the overview, and the full
   diff when it fits.
3. The model investigates with the tools. Each turn may issue several calls; results go back with
   their `tool_call_id`. A step budget and a prompt-size budget bound the cost; when either runs out
   the model is forced to submit.
4. The existing discussion (comments, inline threads, review states) is digested into the opening
   message, with a `pr_discussion` tool for the full text, so the model builds on the review rather
   than restarting it.
5. The model ends by calling `submit_review`, a client-side tool whose arguments are the review.
   Findings are validated: a `line` must be on the new side of the diff to become an inline
   comment, otherwise it is demoted to the summary with a note; a file outside the diff is demoted
   too; a malformed submission is rejected with reasons and the model retries.
6. Findings anchored to a changed line are posted as inline review comments at that line, each
   self-contained with problem, trigger, impact, recommendation and a committable suggestion. The
   top-level comment (one per PR, updated in place through a hidden marker) carries the verdict,
   summary, test gaps, commit-message and discussion issues, the test plan, and any finding that
   could not be anchored. A re-run skips inline comments it already posted for the same finding at
   the same location, so threads stay single. If GitHub rejects the inline review, the findings
   are folded back into the top-level comment. `post_inline_comments: "false"` puts everything in
   the top-level comment. `review-output/` receives `comment.md`, `inline_comments.json`,
   `review.json` and `transcript.jsonl`.

### Test plan (option)

With `generate_test_plan: "true"` (or `--test-plan`), the review also proposes the twister runs
that would exercise the change, with platform coverage that matches what changed. The model works
from a `test_candidates` tool that reimplements the signals Zephyr's own `scripts/ci/test_plan.py`
uses, without executing anything from the tree: suites the PR edits (including the `common`
directory rule), MAINTAINERS `tests:` identifiers, tags from `scripts/ci/tags.yaml`, suites
covering the changed source, and the platforms implicated by changed boards (via board.yml and
DTS file names), SoCs, architectures and the boards whose devicetree instantiates a changed
driver's compatible. A `list_platforms` tool gives valid platform identifiers.

Each entry names a suite, optionally one scenario, platforms, a priority and what the run
verifies. Before publishing, the plan is checked against the tree: unknown suites, misspelled
scenarios (with the closest match suggested), unknown platform identifiers, and platforms a suite's
`platform_allow` or `platform_exclude` rules out are flagged in the comment. The comment carries a
table and ready-to-run `west twister` commands; `review-output/test_plan.yaml` holds the same plan
for automation.

### Two-stage review (advanced)

Investigation turns dominate cost: the whole transcript is resent on every call. Setting
`draft_model` makes a low-cost model do that work and a stronger one finish it:

```yaml
with:
  model: anthropic/claude-sonnet-4.5     # verifies and finalizes
  draft_model: google/gemini-2.5-flash   # investigates and drafts
  verify_max_steps: "15"
```

Stage 1 runs the normal review with the cheap model. Stage 2 receives the same opening message plus
the draft review and a dossier of the evidence stage 1 gathered (deduplicated tool results, highest
value first, within `dossier_chars`), keeps live tool access with its own smaller step budget, and
is told to confirm, correct or drop every draft finding against the code, then spend what remains
on the riskiest regions the draft may have missed. Only the final review is published;
`review.json` and the step summary record both stages' models, steps and cost. If the cheap model fails to
submit, stage 2 runs as a full single-stage review, so the option can only cost, never degrade.

Expect the saving to be large when the draft model is an order of magnitude cheaper and the
verifier needs few steps; measure it on your own pull requests before relying on it, and watch
the verifier's drop rate: a draft model that is wrong most of the time is not saving anything.

### Existing discussion

The reviewer reads what has already been said. Before the model starts, the orchestrator fetches
the conversation, the inline review threads (grouped, with file and line, outdated ones marked),
and each reviewer's latest review state, and puts a bounded digest in the opening message. The
full text is available through a `pr_discussion` tool, filterable by file. The previous automated
review, if one exists, is shown separately and labelled as not being evidence.

The profile tells the model how to use it: a concern a maintainer already raised is theirs, not a
new finding; a request the head commit satisfies is resolved; author replies are checked against
the code rather than taken on trust. Only problems are reported, in an "Unresolved discussion"
section of the comment: a request the head commit does not address, or a claim the code
contradicts. Turn it off with `include_discussion: "false"`.

### CI results

The model sees the head commit's check runs and commit statuses in the opening message and can
call `ci_status` for the full list and `ci_failure_logs` for the tail of a failed GitHub Actions
job's log (timestamps and colour codes stripped, optional grep with context). It is told not to
repeat what CI already reports but to diagnose a failure when it can tie it to the change, and never
to treat a green CI as proof. Needs `checks: read` and `actions: read` on the job token; disable
with `include_ci: "false"`.

### Untrusted text and lockdown

Everything the model reads from GitHub was written by the people whose code it reviews, or by
their tooling. The reviewer treats it that way, following the GitHub MCP server's lockdown idea:

- Every author is tagged by repository permission: maintainer (admin/maintain), collaborator
  (write/triage), bot (a short list of known automation), external, or unknown when the token
  cannot see permissions. The PR author's trust is shown in the opening message.
- HTML comments and zero-width or control characters, which GitHub renders invisibly, are
  stripped from every body before it reaches the model.
- PR description and discussion are wrapped in explicit data delimiters and the working rules say
  never to follow instructions found in them, and to verify their claims against the code.
- `lockdown: strict` additionally withholds comments from external users other than the PR author.
  `label` is the default; `off` disables tagging.

Safety settings follow a tightening-only rule, as the GitHub server does with its lockdown header:
the operator's values (workflow inputs, or server flags for the future shared service) are a
floor, and a request (CLI flags, or per-request headers later) may make them stricter but never
looser. A rejected relaxation is logged and ignored. See `src/zephyr_reviewer/review/policy.py`.

### Typed tool outputs

Tools whose results programs will consume, `pr_overview`, `maintainers`, `list_platforms` and
`find_definition`, publish an output schema and return structured content alongside the prose the
model reads. The remaining tools return text; they gain schemas as consumers appear.

### Run details

The posted comment ends with the one-line advisory notice and nothing else. Models, steps, tool
calls, tokens, cost and time go to the job's step summary, the log and `review.json`. Set
`show_run_details: "true"` (or `--show-details`) to append them to the comment.

### Issues only

The review reports problems and nothing else. The summary states the result and the key issue
rather than describing the change. The test, commit-message and discussion assessments appear in
the comment only when something is missing or wrong; the schema and profile say so, and a filter
drops short all-clear boilerplate ("tests are adequate", "no issues found") should a model write it
anyway. A clean pull request gets a short approval.

### Model selection

OpenRouter does the routing. `model` is the primary slug and `fallback_models` are tried in order
by OpenRouter itself (the request carries the `models` array). `provider` passes
[provider routing preferences](https://openrouter.ai/docs/features/provider-routing) through
unchanged, so you can require `{"data_collection": "deny"}` or pin an ordering. Transient failures
retry with backoff and honor `Retry-After`.

## GitHub Actions

`action.yml` is a composite action. [examples/zephyr-ai-review.yml](examples/zephyr-ai-review.yml)
is a workflow to drop into the Zephyr repository or a fork. Points that matter:

- It uses `pull_request_target` so forked pull requests can reach the OpenRouter secret. That is
  safe only because the workflow checks out the base branch, the action fetches the head by SHA,
  and nothing from the pull request is executed. Do not add a head checkout or a build step to
  that job.
- A shallow checkout is enough; the action fetches missing commits itself. Depth only improves
  `file_history` and `blame`.
- The example gates on an `ai-review` label so cost is opt-in. Remove `required_labels` to review
  everything.
- `fail_on_severity` turns the step into a gate. Leave it at `none` until the false-positive rate
  is known.

Inputs and outputs are documented in [action.yml](action.yml).

### Zephyr's AI policy

Zephyr's contributor guidelines ask that review comments written with AI help be verified by a
human before posting. Every comment this tool posts is labelled as model-generated and advisory,
and `dry_run` plus the uploaded artifacts let a maintainer read the review before anything reaches
the pull request. Decide with the project how it should be used before enabling posting on the
upstream repository.

## Layout

| Path | Purpose |
| --- | --- |
| `src/zephyr_reviewer/intel/repo.py` | Read-only git access pinned to a commit |
| `src/zephyr_reviewer/intel/diff.py` | Unified diff parsing, new-side line sets |
| `src/zephyr_reviewer/intel/symbols.py` | Definition and reference heuristics for C, Kconfig, DT, CMake |
| `src/zephyr_reviewer/intel/kconfig.py`, `devicetree.py`, `maintainers.py`, `tests_index.py`, `platforms.py` | Domain lookups |
| `src/zephyr_reviewer/intel/service.py` | `SourceIntel`: the facade the tools are written against |
| `src/zephyr_reviewer/mcp_server.py` | The MCP server (stdio or streamable HTTP) |
| `src/zephyr_reviewer/review/mcp_client.py` | Spawns or connects to the server; tool schemas for the model |
| `src/zephyr_reviewer/review/openrouter.py` | Chat completions with tools, fallback routing, retries |
| `src/zephyr_reviewer/review/agent.py` | The loop: tool calls, budgets, `submit_review` |
| `src/zephyr_reviewer/review/schema.py` | Review schema and validation against the diff |
| `src/zephyr_reviewer/review/ci.py` | Check runs, statuses and failed-job log tails as tools |
| `src/zephyr_reviewer/review/policy.py` | Safety settings a request may tighten but never relax |
| `src/zephyr_reviewer/review/testplan.py` | Validates a proposed test plan against suites, scenarios and platforms |
| `src/zephyr_reviewer/review/stages.py` | Two-stage review: draft with a cheap model, verify with a strong one |
| `src/zephyr_reviewer/review/discussion.py` | PR comments and threads with author trust and resolution state; digest and `pr_discussion` tool |
| `src/zephyr_reviewer/review/render.py`, `publish.py`, `github_api.py` | Comment rendering and delivery |
| `src/zephyr_reviewer/cli.py` | `serve`, `review`, `tools`, `call` |
| `tests/` | A miniature Zephyr-shaped repository fixture; intel, MCP (in-process and stdio), agent, publish tests |

## What is not done

- No live run yet. The agent is exercised against a scripted OpenRouter; the first real pull
  request will calibrate `max_steps`, cost, and the profile.
- Symbol lookup is regex over `git grep`. It is fast and good enough to start, and wrong on
  unusual formatting. A real index belongs in the shared service.
- The shared service (indexing, review history, project knowledge) does not exist. `review.json`
  and `transcript.jsonl` are written per run so there is history to load when it does.
- Using the GitHub MCP server alongside this one in Claude Code gives a maintainer GitHub state
  plus source intelligence; nothing here duplicates its tools.
- Incremental review of a force-pushed branch (what changed since the last review) is not wired
  in; `gh pr-diff` solves the hard part and is the natural next tool.

## License

Apache 2.0.
