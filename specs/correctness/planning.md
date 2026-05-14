# Correctness Spec — Planning (plan_with_claude)

Given a user's context describing a software problem, feature request, bug,
architecture question, or refactoring need, a correct plan satisfies ALL of
the following properties:

## Required Properties

1. **Addresses the stated context.** The plan directly responds to the problem
   or request described in the inputs. It does not answer a different question.

2. **Actionable steps.** The plan contains concrete, ordered steps that an
   engineer could follow. Vague advice ("consider improving performance") without
   specific actions is a violation.

3. **No hallucinated references.** File paths, function names, API endpoints,
   tool names, library names, and configuration keys mentioned in the plan must
   either (a) appear in the input context, (b) be well-known public APIs, or
   (c) be clearly marked as "to be created." Fabricated references that do not
   exist are a violation.

4. **Structural completeness.** If the context describes multiple sub-problems
   or requirements, the plan addresses all of them or explicitly states which
   are deferred and why.

5. **No internal contradictions.** Steps in the plan must not contradict each
   other. If step 3 depends on step 5's output, that is a violation.

## Not Evaluated

- Subjective quality of the plan (elegance, brevity, style).
- Whether the plan is the *best* approach — only whether it is a *valid* one.
- Cost or token efficiency of the underlying model call.
