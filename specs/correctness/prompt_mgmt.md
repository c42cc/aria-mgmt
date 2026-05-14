# Correctness Spec — Prompt Management (edit_prompt, rollback_prompt)

Given a request to view, edit, or rollback a prompt template, a correct result
satisfies ALL of the following properties:

## Required Properties

### For `edit_prompt`

1. **Structural integrity.** The edited prompt must be valid markdown. The edit
   must not corrupt the file (e.g., truncating it, introducing broken syntax,
   removing all content).

2. **Change alignment.** The edit should reflect the user's stated instruction.
   If the user said "make it more concise," the result should be shorter, not
   longer. If the user said "add a section about X," that section should exist.

3. **Preservation of unrelated content.** Parts of the prompt not addressed by
   the edit instruction should remain unchanged. Wholesale rewrites when only
   a targeted change was requested are a violation.

### For `rollback_prompt`

1. **Version accuracy.** The restored content must match the exact content of
   the specified version number. Restoring a different version is a violation.

2. **Confirmation.** The result should confirm which version was restored.

### For read operations (`show_prompt`, `list_prompts`, `prompt_versions`)

1. **Accuracy.** The returned data must match what exists on disk. Fabricated
   prompt names or version numbers are a violation.

## Not Evaluated

- Quality of the prompt content itself.
- Whether the edit improves system behavior.
