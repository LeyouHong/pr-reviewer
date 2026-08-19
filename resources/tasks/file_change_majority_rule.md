{role_prompt}

You are provided a collection of general and language-specific guidelines, and a collection of reviews made independently by LLM agents. Please aggregate the reviews and generate a tone-neutral final review based on majority rule aggregation.

## Pull Request Context

- Project:       {mr_project}
- Title:         {mr_title}
- Source Branch: {mr_source_branch}
- Target Branch: {mr_target_branch}

### Description

````markdown description
{mr_description}
````

### Reviews of File Changes for `{filepath}`

{review_list}

## Context and Directives

### Aggregation Context

Majority rule-based review aggregation is the summarization of multiple agent reviews into a single, consolidated review through a majority rule voting system. By choosing issues that are agreed on by the majority (more than half) of agents, we minimize the noisy errors produced by individual agents, and prevent the urgent tone of individual agents from bringing developer fatigue.

#### Definitions

Two comments are *similar* if they satisfy BOTH conditions:

- discuss similar topics, bugs, or issues,
- reference similar line numbers (within 3 lines) as reported by the individual reviews

A *topic* is what a comment discusses, is identified by approximate line numbers, and can be shared across different agents' comments. A topic satisfies *majority rule* if the number of similar comments about it is at least {majority_threshold}.

#### Majority Rule Algorithm

- If a topic satisfies majority rule, include it as a comment in the final review. Each agent's comments count only once; similar comments under the same agent count as one comment.
- The final message is a brief summarization of the similar comments, with priority towards the *most detailed or helpful* one.
- The final severity is the **minimum** of the severities in the constituent comments.
- The final category is decided by which category appears in the **majority** of similar comments.
- The final criteria violated is decided by which criteria appears in the majority of similar comments.

Otherwise, the final numeric value should be the *most common* value. The final sentence value should be a summarization, priority toward the most detailed one.

#### Example: Applying Majority Rule

3 agents produce:

````
# agent 1
- L12+ buffer overflow issue (error)
- L88+ styling issue, should rename "ersion" to "version" (warning)

# agent 2
- L13+, L11+ a buffer overflow introduces potential security vulnerability (error)
- L1100+ critical dangling pointer (error)

# agent 3
- L13+ dangling pointer error (error)
- L87+, L88+ a typo in "ersion", it should be "version". (error)
````

4 topics:

- Topic 1 (L12+ buffer overflow): agents 1 and 2, majority, severity error (minimum)
- Topic 2 (L88+ typo): agents 1 and 3, majority, severity warning (minimum)
- Topic 3 (L1100+ dangling pointer): agent 2 only, not majority, dropped
- Topic 4 (L13+ dangling pointer): agent 3 only, not majority, dropped

Final report:

````
# final comment
- L12+ buffer overflow issue introduces a potential security vulnerability (error)
- L88+ there is a typo in "ersion", it should be renamed to "version" (warning)
````

Urgency words such as "critical" from the input are removed in the final report.

### Aggregation Directives

#### Directive: majority-role

- Comments or topics that belong to no agent's review are prohibited, no matter how serious the issue is. Summarize existing reviews only.

#### Directive: majority-before-severity

"Severity NEVER overrides majority."

- Even a highest-severity report MUST be excluded if the topic does not satisfy majority rule. Including it is a policy violation.
- Special case: if all other agents have no comments and one lone agent reports a "critical" error, exclude it.

#### Directive: majority-encapsulation

When writing any sentence in the final review, mentions of "majority rule", "agents", "consensus", "agreement", "overall", or "most agents agree" are prohibited. Keep the comment strictly as a summarization of the constituent comments only.

Wrong: "All agents agree that the implementation exposes a problem X."
Right: "Implementation exposes a problem X."

#### Directive: majority-consolidation

- If multiple valid comments describe the same issue type at different locations, consolidate them into a single comment entry, merging their line numbers.

#### Directive: majority-urgency

Urgency words ("critical", "serious", "top priority", "must", "have to", "ASAP") are prohibited from any review.

Wrong: "Critical buffer overflow vulnerability is a serious problem and can crash entire system. Fix ASAP."
Right: "Buffer overflow vulnerability can crash system."

The tone of the final issue MUST be neutral and informational only.

## Response Structure Constraint

Report the aggregated review by calling the `submit_file_review` function exactly once.
