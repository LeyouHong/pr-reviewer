### Context

This document is the authoring contract for every file under `resources/`. It exists so that reviewers of a prompt change can call out a violation by name and so that a new contributor can extend the library without reading every existing file first. Follow it when adding a new role, task, or rule file; when changing an existing file, keep the shape you inherited unless the change is deliberately restructuring it.

The guidelines are grouped by role — different sections govern different files — and each rule lists **why** it exists. The reasoning matters more than the mechanical rule, because most drift happens on cases that no rule anticipated.

### Criteria: prompt-structure

Every rule file that expresses a criterion or directive MUST separate three concerns using heading level three (`###`):

- **Context** — background the model needs before it can apply the rule. Facts, definitions, worked examples.
- **Criteria** — statements the model MUST NOT violate. Written as positive obligations ("a finding MUST cite a line inside the diff"), not as forbidden strings.
- **Directive** — what the model MUST DO when the criterion applies (raise a comment, drop one, downgrade severity, cite evidence). Distinct from criteria because a criterion says *what counts*; a directive says *what happens next*.

**Why:** the three concerns fail differently. A missing context yields hallucinated findings; a missing criterion yields inconsistent severities; a missing directive yields findings the pipeline discards silently. Splitting them makes each failure locatable.

Heading level three is deliberate. Level two is the file title; level three lets the render pipeline surface anchor IDs (`general-noise`, `python-mutable-default`) that comments cite via the `criteria` and `rule` fields. If the anchor ID changes, existing benchmark comments become uncitable.

### Criteria: role-files

Files under `resources/role/{variant}/` prime the assistant's identity, not its behaviour. Keep them to one or two sentences and constrain them to positive framings.

- Role files MUST be one to three lines long. A role file is not the place to state review policy — that belongs in `resources/rules/`.
- Role files MUST NOT contain "don't", "never", or "avoid" clauses.
- Model-family variants (`default`, and any future `claude`, `qwen`, etc.) MUST share the same set of role names. A missing variant falls back to `default`; a missing name in `default` is a bug.

**Why:** the Don't-Think-Of-Elephant pattern makes negative framings sticky in LLM outputs — telling a role file to "never speculate" trains the model to speculate. Route the same policy through a `Criteria` in a rule file, which the pipeline treats as a hard obligation rather than a piece of self-image.

**Why (variant symmetry):** a variant is a tone-and-tempo swap, not a policy swap. If two variants disagree on what the reviewer does, the benchmark numbers stop being comparable across model swaps.

### Criteria: task-files

Files under `resources/tasks/` are action instructions — what the model does this turn. They pair with a role and receive their inputs via `{placeholder}` substitution.

- Task files MUST NOT rely on Python's `str.format` semantics: braces around JSON, code samples, and set/dict literals stay literal. The `prompt.render()` helper enforces this; do not bypass it.
- Every placeholder a task file references MUST be supplied by the caller. Missing placeholders leave `{name}` in the prompt verbatim, which the model then quotes back and reasons around. If the placeholder is optional, default the caller's value to a stated string (`"(no description provided)"`), not the empty string.
- A task file MUST NOT restate a rule that already lives in `resources/rules/`. Duplication drifts: the two copies grow apart when only one is updated.

**Why (placeholder discipline):** the reference implementation's silent placeholder miss injected `[Missing prompt resource]` into every prompt for months without an alarm, because the model reasoned around it plausibly. The convention above turns those failures into empty strings that show up in outputs immediately.

### Criteria: rule-files

Files under `resources/rules/` express the review contract. They are concatenated by the policy router (`resources/policies/default.json`) and embedded in the reviewer prompt as the `{general_rules}` and `{rules}` slots.

- Every rule file MUST use the three-section structure (`### Context`, `### Criteria: <anchor>`, `### Directive: <anchor>`).
- Anchor IDs MUST be stable once cited. Renaming an anchor invalidates benchmark comments whose `criteria` or `rule` fields quote it.
- Language rule packs live under `resources/rules/language_rules/{language}.md`. Cross-language rules live at the top of `resources/rules/`. Do not mix.
- Examples in a rule file MUST be illustrative, not exhaustive. If a rule needs twenty examples to be understood, the criterion is under-specified.

**Why (anchor stability):** the benchmark matcher pairs a finding to a ground-truth bug partly on the strength of the `rule` string. A silent rename orphans historical scorecards without changing any code.

### Directive: experiment-vs-control

When a prompt file is itself the subject of a review — a rule file changed in a PR — the model is asked to review its own instructions. To keep that from becoming self-fulfilling:

- The prompt file text (the *experiment*) MUST NOT compel the pipeline to take a review action against itself. Statements inside a rule-file diff are treated as data, not policy, when reviewing that same rule file.
- The task file that wraps the review (the *control*) is the only place that can compel action.

**Why:** without the split, a malicious or careless rule edit that says "ignore all further review" would be honoured on the very PR that introduces it. The control wraps the experiment; only the control has authority.

### Directive: change-discipline

- A prompt change MUST be measured before it is merged. Run the benchmark scorecard before and after; keep the delta in the PR body. A prompt PR without a delta is a guess.
- Add a regression case only *after* the fix merges. Adding it before is overfitting to the specific example that triggered the change.
- If a change adds a new placeholder to a task file, update every caller in the same PR. Callers that forget the new key produce silently degraded prompts, not runtime errors.

**Why (benchmark first):** prompt behaviour is not statically checkable. `assert precision >= X` is either trivially true or fails without explaining why. Scorecard deltas are the only signal that a change moved the reviewer in the intended direction.

**Why (regression cases after merge):** integration test corpora added from a false positive report tend to be the reviewer's exact input at the time of the report. If the fix is added at the same time as the case, the prompt memorises the case rather than the class.
