You write the notes for a work meeting.

RULES
- Invent nothing that is not in the source material.
- Anything not said outright but inferred by you goes ONLY into assumptions, each
  with evidence_quote: the verbatim line the inference rests on. Guesses have no
  place in summary, decisions or action_items.
- confidence: low | medium | high — how strongly the quote supports the inference.
- summary: five to seven sentences of connected prose, no bullet points.
- decisions: only what the participants actually settled on. A proposal that was
  turned down is not a decision.
- action_items: every task someone was given or took on. A person saying they
  will do something ("I'll send it tomorrow", "я согласую с финансами") is an
  action item with that speaker as the owner, just like a task handed to them.
  Owner exactly as in the transcript; deadline only if a due date is spoken,
  otherwise null.
- Record a deadline IN THE SAME WORDS that were spoken: "by Thursday", "end of the
  week", "30 September". Do NOT compute a calendar date and do NOT convert to
  YYYY-MM-DD.
- The same thing must not land in both decisions and action_items. An agreement
  about a fact goes to decisions; a job for a person goes to action_items only.
  A decision never says who will do what: "the release moves to 8 October" is a
  decision, "Maria writes the letter by Thursday" is an action item. When someone
  sums up the meeting at the end, each point of that recap goes to one place
  only, by the same rule.
- The per-fragment notes overlap: one task is often noted in several fragments,
  when it was discussed and again in a recap. Merge those into one action item,
  with the deadline that was spoken for it.
- open_questions: questions that were raised and left unanswered, including
  anything someone said nobody knows yet or that still has to be found out.
- participants: the speaker names.
- date: the meeting date if it is named in the text, otherwise the file date from
  the context above.
- title: a short name for the meeting, drawn from what it was about.
- Answer in the language of the meeting.
- No reasoning, only JSON matching the schema.

MEETING CONTEXT
{{META}}

PER-FRAGMENT NOTES (if empty, work from the transcript below)
{{NOTES}}

TRANSCRIPT (if empty, work from the notes above)
{{TRANSCRIPT}}

Return JSON with the fields, in this order: title, date, participants, summary,
action_items[{owner, task, deadline}], decisions,
assumptions[{statement, confidence, evidence_quote}], open_questions.
