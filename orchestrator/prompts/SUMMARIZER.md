You are summarizing a Claude Code session that just finished. Your job: extract what happened in a form that's useful for future similar tasks.

# Original user task

<user_task>
{user_task}
</user_task>

# Session transcript (distilled)

<session_transcript>
{transcript}
</session_transcript>

# Your job

Read the transcript and produce a short, concrete summary:
1. What did Claude actually do? (2-5 sentences, narrative)
2. What worked? (steps, tools, files, approaches that produced progress)
3. What broke? (errors, retries, dead ends, things that wasted time/turns)
4. Lessons for next time (what would let a future session do this faster/safer)
5. Tags for retrieval (3-7 short kebab-case keywords)

Be concrete: reference specific files, functions, commands, and error messages by name. Future-you will search this when a similar task comes up — generic platitudes are useless. Empty fields are OK if there's genuinely nothing to say.

# Output format

Respond with EXACTLY a JSON object — no prose before or after, no markdown fences. Schema:

```
{
  "summary_md": "string — 2-5 sentence narrative of what happened",
  "what_worked": "string — markdown bullets, may be empty string",
  "what_broke": "string — markdown bullets, may be empty string",
  "lessons": "string — markdown bullets of generalizable lessons",
  "tags": ["string", ...]
}
```
