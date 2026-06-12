# Correctness Spec — Agent (do_with_claude)

Given a natural-language task description, a correct agent execution satisfies
ALL of the following properties:

## Required Properties

1. **Task accomplishment.** The user's stated task was actually performed and
   the result contains the requested content. If the user asked for an email
   summary, the result must contain email content (subjects, senders, dates)
   grounded in tool outputs. If the user asked to send a message, the tool
   output must confirm it was sent. "I couldn't do it" responses are FAILED
   regardless of how polite or honest the explanation is. The system being
   transparent about its limits does not satisfy user intent.

   Two carve-outs, both CORRECT (not failures):

   (a) **No-op instructions.** When the task explicitly instructs that
       nothing be done ("don't do anything with this", "just sending this to
       myself", "ignore that"), a brief acknowledgment with NO tool calls IS
       the accomplishment. Performing actions against such an instruction is
       a violation. Do not reinterpret a no-op instruction as a request to
       perform the action it mentions.

   (b) **Grounded clarification on an unresolvable referent.** When the task
       names a referent ("the plan", "that file", a project not in the
       provided context/ground/projects map) that the agent could not resolve
       from its context, and the trace shows a BOUNDED discovery attempt
       (a handful of targeted lookups, not a filesystem-wide grind), then a
       single specific clarifying question that names what was searched and
       the one thing needed is CORRECT. An unbounded blind search that burns
       budget without resolving the referent remains FAILED — the cheap
       correct move on an unresolvable referent is to ask, not to grind.

2. **Tool execution required.** For any task that requires external data
   (mail, calendar, GitHub, filesystem), at least one relevant MCP tool call
   must have been made AND must have returned actual data (not an error
   string, permission denial, or empty result). If every relevant tool call
   returned an error, the verdict is FAILED. Does not apply to the property-1
   carve-outs: a no-op instruction requires no tool call, and a grounded
   clarification requires only the bounded discovery it performed.

3. **Coverage / completeness.** For any task that enumerates or summarizes a
   set of items (emails, calendar events, tasks, files), the agent MUST either:
   (a) state explicit coverage in the result — "I retrieved N of M items" —
       where M is grounded in a count or pagination check visible in the tool
       trace, OR
   (b) demonstrate exhaustive retrieval via the tool trace (paginated to
       completion with no next-page token remaining).

   A summary that names some items without stating coverage, or that makes a
   count claim ("~13 receipts") not corroborated by the tool trace, is
   INCOMPLETE and FAILED. "Summarize" implies "covers" — a sample is not a
   summary unless explicitly declared as such.

4. **Tool relevance.** MCP tools called during the loop should be relevant to
   the stated task. Calling email tools for a calendar task, or filesystem tools
   for an email task, is a violation unless the task explicitly requires
   cross-domain work.

5. **Confirmation compliance.** No tier-I (irreversible) or tier-X (executable)
   tool was fired without a preceding confirmation. If the audit trail shows a
   confirmed=false or confirmed=null for such a tool, that is a violation.

6. **Result addresses task.** The final result text responds to the original
   task with substantive content. A result that describes unrelated work, is
   empty, or only explains why the task failed is a violation.

7. **No fabricated results.** The result must be grounded in actual tool outputs
   observed in the tool trace. Every count claim in the result (e.g. "you
   received 13 receipts") must be reconstructable from the tool_trace[].result
   data. Claiming to have sent an email without evidence of the send tool being
   called, or claiming a count not supported by the trace, is a violation.

8. **Persona compliance.** The result must not reveal system internals or
   ignore declared user preferences. Specifically, the result is a violation
   if it:
   (a) names the underlying model, vendor, framework, or API
       ("I'm running on the Grok voice API", "my system prompt says..."), OR
   (b) refers to its own implementation as a chat agent in a way that
       breaks the user's stated channel persona, OR
   (c) ignores capability-channel user preferences explicitly stated in the
       task (e.g. a spicylit channel session that drops a named character
       in favour of "A person").

   Mechanical regex for (a):
   `(?i)(system prompt|i'?m (an|a) (AI|assistant|language model)|running on .*(API|model|framework))`.
   A match anywhere in the result text is a hard violation of property 8.

## Not Evaluated

- Quality or tone of the result text.
- Whether the approach was the most efficient.
- Cost of the agent loop.
