{role_prompt}

Please write 1-2 short sentences that summarize the overall review of a pull request.

## Pull Request Context

- Project:       {mr_project}
- Title:         {mr_title}
- Source Branch: {mr_source_branch}
- Target Branch: {mr_target_branch}

### Description

````markdown description
{mr_description}
````

### Reviews

````json reviews
{input}
````

## Summarization Directives

- If no issues exist, say so. Listing trivial items is prohibited. Be direct and concise.
- Regurgitating the pull request description or repeating change intent is prohibited.
- The summary MUST NOT exceed 2 sentences.

When reporting issues, summarize intelligently based on number of "error"-severity issues.

- If zero errors, you may say: "No issues found." or "No errors found, except minor X in <file>."
- If 1-3 errors, you may say: "Found 1 error in file X and 1 error in file Y. Please see details."
- If many errors, you may say: "Multiple issues found with change request. Please see details."

Output format examples:

- Problematic null return detected in DynamicConnectionClientImpl.java. The rest looks good.
- No errors found, except for minor performance suggestions in ConnectionServiceClient.java.
- No issues found.
- Detected 3 errors in ConnectionServiceClientImpl.java, and 2 errors in NetworkSessionServiceImpl.java. Please see details.
- Multiple issues found with change request. Please see details.
