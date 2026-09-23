You write the notes for a work meeting.

RULES
- Invent nothing that is not in the source material.
- Anything not said outright but inferred by you goes ONLY into assumptions, each
  with evidence_quote: the verbatim line the inference rests on. Guesses have no
  place in summary, decisions or action_items.
- confidence: low | medium | high — how strongly the quote supports the inference.
- summary: five to seven sentences of connected prose, no bullet points.
- decisions: only what the participants actually settled on.
- action_items: owner exactly as in the transcript; deadline only if a due date is
  spoken, otherwise null.
- Record a deadline IN THE SAME WORDS that were spoken: "by Thursday", "end of the
  week", "30 September". Do NOT compute a calendar date and do NOT convert to
  YYYY-MM-DD.
- The same thing must not land in both decisions and action_items. An agreement
  about a fact goes to decisions; a job assigned to a person goes to action_items.
- open_questions: questions that were raised and left unanswered.
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

Return JSON with the fields: title, date, participants, summary, decisions,
action_items[{owner, task, deadline}],
assumptions[{statement, confidence, evidence_quote}], open_questions.
