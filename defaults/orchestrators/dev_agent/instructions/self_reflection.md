---
id: self_reflection
name: Self-Reflection
description: Conducts a structured post-task self-reflection: difficulties, root causes, and concrete improvements for DevAgent's prompt, tools and skills.
---

# Self-Reflection -- Post-Task Analysis

Conduct a self-reflection on the task that was just completed. The goal is to
understand what prevented you from working more efficiently and to produce
concrete improvements for future tasks.

## 1. Task context
- Briefly: what task you solved, which files/components you touched.
- How many steps (read -> plan -> edit -> verify) it took.

## 2. What went well
- List 2-3 things that worked smoothly: strategy choice, tool usage, tests,
  simulations, etc.

## 3. Difficulties encountered
- Describe each difficulty as a separate bullet:
  - what happened,
  - at which step,
  - the root cause (unknown API, wrong assumption, tool limitation,
    escaping error, state recreation, context loss, etc.),
  - how many extra cycles/tokens it cost.

## 4. Mistakes and their causes
- For each mistake indicate:
  - type (syntactic, logical, tool-related, process-related),
  - what exactly you did wrong,
  - what rule/check could have prevented it.

## 5. What can be improved in DevAgent's work
Split suggestions by category:
- **System prompt**: which rules to add/refine/remove (with a concrete
  candidate wording).
  Examples: thresholds for reading files, rules for run_code vs scratch,
  banning extra steps, handling nested quotes, keeping a cache anchor
  between cycles.
- **Tools/functions**: which tools to add, change or improve (with an example
  signature and expected behaviour).
  Examples: apply_patch for surgical edits, structured errors with
  suggestions, remaining/hint in read_file, path existence check inside
  run_code/run_test.
- **Skills**: which new skills could speed up typical scenarios (e.g. "surgical
  edits in large files", "post-task reflection"), which existing ones to refine.
- **Tests**: which regression tests to add to lock in the found pitfalls.

## 6. Priority changes
- Pick the 3 most important improvements and justify why they will give the
  biggest effect.
- For each, indicate approximate complexity (easy/medium/hard) and the area of
  responsibility (prompt/tools/skills).

## 7. Conclusion
- 2-3 sentences: what I will take from this experience next time, how I will
  change my approach.

## Rules
- Be honest and specific: no generic phrases, use examples from the actual
  thread.
- Estimate how many steps/tokens could have been saved if the proposed
  improvement existed (at least approximately).
- If the difficulty was caused by your own mistake and not by a platform
  limitation, say so; do not shift blame onto the tools.

## Analyzing your own code
For accurate diagnostics you are allowed to analyze DevAgent's code: switch to
its working folder (e.g. `set_workspace('<SagaAI root>')`) and read files
(`read_file`, `list_files`, grep-like checks via `run_code`) to understand the
real cause of an error (function logic, argument handling, state between calls,
etc.).

This is **read-only**: during self-reflection you are FORBIDDEN from editing
DevAgent's code, creating/modifying files, calling write tools (`propose_file`,
`apply_edit`, `write_doc`, `write_project_map`, `snapshot_all`, etc.), or
installing/updating skills.

All found problems and fix proposals belong in sections 4-6 as recommendations.
Actual edits are performed only after a separate explicit request from the user.
