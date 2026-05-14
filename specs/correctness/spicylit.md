# Correctness Spec — SpicyLit

Given a storytelling session where the user provides preferences and the system
generates an outline and narration, a correct session satisfies ALL of the
following properties:

## Required Properties

1. **Preference capture.** The generated outline incorporates the preferences
   and themes the user expressed during the session. Ignoring stated preferences
   is a violation.

2. **Structural completeness.** The outline contains distinct narrative beats
   with a beginning, development, and conclusion. An outline that is a single
   paragraph with no structure is a violation.

3. **Persona adherence.** The narration and outline stay within the storyteller
   persona defined by the system. Breaking character to discuss system internals,
   other capabilities, or unrelated topics is a violation.

4. **User name usage.** If the user provided a protagonist name, the outline
   uses it consistently. Using a different name or no name when one was provided
   is a violation.

5. **Continuation coherence.** If the session is a continuation of a previous
   story, the new outline should reference or build on the prior outline's
   characters and events. Starting a completely unrelated story when continuation
   was requested is a violation.

## Not Evaluated

- Literary quality or creativity of the story.
- Whether the user enjoyed the narration.
- Audio quality or pacing of the voice output.
