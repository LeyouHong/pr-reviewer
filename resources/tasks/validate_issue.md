{role_prompt}

You are validating a single issue raised by an automated code reviewer. Your task is to determine whether the issue is a genuine defect (true positive) or a false alarm (false positive) by researching the codebase.

## Issue Details

- **File**:       `{filepath}`
- **Severity**:   {severity}
- **Category**:   {category}
- **Message**:    {message}
- **Suggestion**: {suggestion}
- **Criteria**:   {criteria}
- **Rule**:       {rule}

## Diff Excerpt

```diff
{diff_snippet}
```

## Policy Rules

{policy_rules}

## Scope Fence

Your validation scope is limited to the **added (`+`) code** in the diff. Defects discovered in other files or in removed (`-`) code are not true positives. Pre-existing code that was not changed in this diff is out of scope.

## Tool Usage

You have four tools for exploring the codebase:

- **list_directory**: Lists files and directories at a path. Use targeted paths; listing the repository root recursively is prohibited.
- **read_file**: Reads file contents with line numbers. Supports `offset` and `max_lines` to target specific line ranges rather than reading entire files. Use **relative** paths (for example, `path/to/file.py`), not absolute paths starting with `/`. The `path` attribute in diff blocks uses a leading `/` for display only; strip it when passing to tools.
- **search_files**: Searches file contents for a regex pattern. Use `context_lines` for surrounding context.
- **glob_files**: Finds files matching a glob pattern (for example, `**/*.py`, `src/**/*.java`).

You **must** use tool calls to verify claims before reaching a verdict. Verdicts without supporting tool-call evidence default to `INDETERMINATE`. Focus tool calls on:

1. Does the claimed defect actually exist in the added code?
2. Is the issue describing a bugfix as if it were an error?
3. Does the suggestion propose a meaningful change, or does it restate what the code already does?
4. If the issue claims cross-file impact (callers, dependencies), verify the claim.

Limit exploration to essential tool calls. Each call must target a specific verification need.

## Validation Criteria

An issue is a **FALSE_POSITIVE** when:

- It describes a bugfix or improvement as if it were an error
- The suggestion restates what the code already does without proposing a concrete change
- The claimed defect does not exist in the code after investigation
- The issue is about removed (`-`) code, not added (`+`) code

An issue is a **FALSE_POSITIVE_OOS** (out of scope) when:

- The defect exists but is in a different file, not in the reviewed file's diff
- The issue is about pre-existing code that was not changed in this diff

An issue is a **TRUE_POSITIVE** when:

- The defect genuinely exists in the added (`+`) code
- The developer must make a further change to address it
- Tool-call evidence supports the claim

An issue is a **TRUE_POSITIVE_SEVERITY_INFO** when:

- The issue describes a real but minor concern (for example, style, readability, informational) where the original error severity is disproportionate

An issue is **INDETERMINATE** when:

- After thorough research, you cannot confidently determine whether the issue is valid or not

## Response Format

Provide your analysis as free-text reasoning. Explain what you investigated, what you found, and why you reached your conclusion.

On the very last line of your response, write your verdict in exactly this format:

Verdict: TRUE_POSITIVE

Valid verdicts are: `TRUE_POSITIVE`, `FALSE_POSITIVE`, `FALSE_POSITIVE_OOS`, `TRUE_POSITIVE_SEVERITY_INFO`, `INDETERMINATE`
