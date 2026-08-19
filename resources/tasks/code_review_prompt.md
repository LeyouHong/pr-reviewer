{role_prompt}

Please review the following file changes made in the context of a larger pull request, according to general and language-specific guidelines.

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

The gutter is the authority on line numbers. When citing a line, use the number that appears in the gutter, and set `line_number_state` to `diff-added` for `+` lines, `diff-removed` for `-` lines, and `file-context` for unchanged lines. A citation that does not appear in the gutter is automatically rejected.

{general_rules}

{rules}

## Response Structure Constraint

Report your review by calling the `submit_file_review` function exactly once. If no defects exist in the added code, pass an empty `comments` array and describe the change in the summary only.
