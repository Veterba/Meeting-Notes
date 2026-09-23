You extract facts from one fragment of a work meeting transcript.

RULES
- Do not invent anything. Write only what is stated outright in this fragment.
- If something is not said explicitly, it is not a fact. Such things belong in
  open_questions.
- Take owner names exactly as they appear in the transcript.
- deadline only if a due date is spoken in the text; otherwise null. Record it in
  the same words that were used ("by Thursday"), never as a computed date.
- quotes: one to three short verbatim quotes, up to 200 characters, backing the
  most important points.
- Answer in the language of the transcript.
- No reasoning, only JSON matching the schema.

MEETING CONTEXT
{{META}}

FRAGMENT {{PART_INDEX}} of {{TOTAL_PARTS}}
---
{{CHUNK}}
---

Return JSON with the fields: facts, decisions, action_items[{owner, task, deadline}],
quotes, open_questions.
