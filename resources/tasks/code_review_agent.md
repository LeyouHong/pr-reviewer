{role_prompt}

Please review the following file change made in the context of a larger pull request, according to general and language-specific guidelines.

You have read-only access to the repository and are expected to use it. The diff alone cannot settle whether a change breaks a caller, contradicts a contract declared elsewhere, or duplicates an existing helper — read the code that answers the question.

## Pull Request Context

- Project:          {mr_project}
- Title:            {mr_title}
- Source Branch:    {mr_source_branch}
- Target Branch:    {mr_target_branch}
- Review Date:      {review_date}

### Description

````markdown description
{mr_description}
````

### File Changes

{mr_file_changes}

## Investigation

Work in two phases.

**Phase 1 — read the diff.** Apply the deterministic scan checks below to every added line. Anything they settle needs no tool call.

**Phase 2 — resolve what the diff cannot settle.** For each remaining suspicion, state the hypothesis to yourself, then make the targeted tool call that confirms or refutes it. Prioritise:

1. **Callers of a changed signature, guard, or return contract.** Use `search_files` for the symbol name. A removed check, a widened parameter, or a new nullable return is only a defect if some caller relies on the old behaviour.
2. **The declared contract of anything the change consumes.** Read the definition before claiming a misuse.
3. **Sibling code that establishes the local convention.** A helper used everywhere else in the module, a guard applied in the adjacent method, an error path handled consistently elsewhere — a change that departs from an established pattern is worth a finding; a change that departs from your preference is not.

Available tools: `list_directory`, `read_file` (supports `offset` and `max_lines`), `search_files` (regex, with `context_lines`), `glob_files`.

Paths are repository-relative with no leading slash. The `path` attribute in the diff block above uses a leading `/` for display only — strip it.

Budget your calls. Each one must answer a stated question; open-ended browsing wastes turns you will need later. Reading nothing at all is the more common failure: a review that makes only diff-local claims will miss every defect that lives in the seam between two files.

## Review Context, Criteria and Directives

A collection of context, criteria, and directives will be presented.

- A **context** is a non-enforceable statement that provides supplementary information.
- A **criteria** is a rule the presented code MUST NOT violate.
- A **directive** is a rule you MUST NOT violate when conducting code review.

Carefully read and follow the directives. Enforce the criteria detailed below on the code being reviewed, and provide suggestions for each violation with an appropriate severity level.

### What constitutes a finding

A finding is a defect, violation, or risk **in the added code** that requires the developer to make a further change. Before emitting each comment, ask: *"Does this comment ask the developer to change something they have written?"* If no, it is not a finding — remove it.

The following are NOT findings and MUST NOT appear as comments:

- Descriptions of bugs in the removed code
- Confirmations that the code is correct
- Narrations of what the change accomplished or why the fix works

A suggestion proposes a concrete change to resolve a defect. If your suggestion instead confirms the code is already correct (for example "The fix correctly does X"), you have no actionable suggestion, and therefore no finding.

### Reading the diff

Each diff line is rendered with a two-column line-number gutter, old file on the left and new file on the right:

        12 |     12  unchanged line
           |     13 +added line
        14 |        -removed line

The gutter is the authority on line numbers. Cite the number that appears there, and set `line_number_state` to `diff-added` for `+` lines, `diff-removed` for `-` lines, and `file-context` for unchanged lines. A citation that does not appear in the gutter is automatically rejected, as is a finding whose every citation lies outside the lines this change added — including one you discovered in another file while investigating.

{general_rules}

{rules}

## Response Structure Constraint

When your investigation is complete, report the review by calling the `submit_file_review` function exactly once. If no defects exist in the added code, pass an empty `comments` array and describe the change in the summary only.

Answering in prose does not end the review. The function call is the only accepted output.
