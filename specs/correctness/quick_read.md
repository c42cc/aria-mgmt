# Correctness Spec — Quick Read (quick_email_check, quick_calendar)

Given a read-only request for email or calendar data, a correct quick-read
result satisfies ALL of the following properties:

## Required Properties

1. **Correct data source.** An email check returns email data; a calendar check
   returns calendar data. Returning calendar data for an email request (or vice
   versa) is a violation.

2. **No fabricated content.** The result must be derived from the actual MCP
   tool response. Invented email subjects, sender names, event titles, or
   timestamps not present in the underlying data are a violation.

3. **Plausible timestamps.** Dates and times in the result should be within a
   reasonable window (e.g., calendar events within the requested lookahead,
   emails within a recent timeframe). Dates far in the past or future without
   explanation are suspicious.

4. **Error transparency.** If the underlying MCP tool returned an error, the
   result must surface that error rather than fabricating a "no results" response.

## Not Evaluated

- Formatting or summarization quality.
- Whether the user actually wanted this specific data.
- Completeness of results (some truncation is acceptable).
