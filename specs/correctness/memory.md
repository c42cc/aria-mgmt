# Correctness Spec — Memory (remember, recall)

Given a memory storage or retrieval request, a correct result satisfies ALL
of the following properties:

## Required Properties

### For `remember`

1. **Intent preservation.** The stored fact should faithfully represent what the
   user stated. Paraphrasing is acceptable; changing the meaning is a violation.

2. **Acknowledgment.** The result should confirm that the fact was stored. A
   silent success with no confirmation is a violation.

### For `recall`

1. **Query relevance.** Retrieved memories should be semantically related to the
   query. Returning facts about cooking when the query is about work contacts is
   a violation.

2. **Faithful reproduction.** Retrieved facts must match what was actually stored.
   Embellishing or inventing details not in the stored memories is a violation.

3. **Graceful empty results.** If no relevant memories exist, the result should
   indicate that clearly rather than fabricating content.

## Not Evaluated

- Ranking quality of recall results.
- Whether the user *should* have stored this fact.
- Deduplication of similar memories.
