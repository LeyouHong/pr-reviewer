{role_prompt}

You are rewriting the summary of a **per-file** code review to reflect the current set of findings for that file.

## Pull Request Context

- Project:       {mr_project}
- Title:         {mr_title}
- Source Branch: {mr_source_branch}
- Target Branch: {mr_target_branch}

### Description

````markdown description
{mr_description}
````

## Original Summary

{original_summary}

## Context: Issues That No Longer Apply

The following issues were determined to be false positives and have been removed from the review. They are shown here only so you understand what changed. References to them in your output are prohibited.

{removed_issues}

## Current Issues

The following issues remain in the review:

{retained_issues}

## Directives

- Rewrite the summary to accurately reflect the **current issues** only.
- Write the summary as if you performed a fresh review and found only the current issues. References to any removed issue, to the validation process, or to filtering are prohibited.
- The summary MUST NOT exceed 2 sentences.
- Follow the same style and tone as the original summary.
- If no issues remain, write: "No defects found in the reviewed code."

Output only the new summary text, nothing else.
