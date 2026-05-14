# Correctness Spec — Build (build_with_cursor)

Given an approved plan and a target project, a correct build satisfies ALL
of the following properties:

## Required Properties

1. **Completion status.** The build reached a terminal state (`completed` or
   `error`). A build stuck in `running` with no events for an extended period
   is a violation.

2. **Plan alignment.** If a plan or instruction was provided in the inputs, the
   build's activity (file edits, test runs) should be consistent with the plan's
   intent. Edits to files or modules not mentioned in the plan require
   justification in the build events.

3. **Project scope.** The build operated within the declared project directory.
   Modifications to files outside the project path are a violation.

4. **Error handling.** If the build status is `error`, the error message in the
   output should be a meaningful description, not a generic or empty string.

5. **Event emission.** The build should have emitted at least one event
   (file_edit, test_run, question, completion, or error) before reaching
   terminal state. A build with zero events is suspicious.

## Not Evaluated

- Whether the generated code is optimal or follows best practices.
- Test pass/fail outcomes (those are tracked separately).
- Build duration or cost efficiency.
