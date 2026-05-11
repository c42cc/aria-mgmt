# Plan and Build Workflow

The default workflow for implementing a feature or change.

1. User describes the task.
2. Gather context: which project, which files, constraints.
3. Call `plan_with_claude` with the appropriate template.
4. Review the plan with the user. Iterate if needed.
5. On approval, call `build_with_cursor` with the plan.
6. Monitor progress. Relay questions. Narrate completion.
