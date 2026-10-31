import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, lines } from "./api.js";

// One meeting's notes as a document: every part editable in place, and the
// way out at the bottom — recipients, save, send.
export default function Notes({ meeting, state, head, refresh, say }) {
  const [notes, setNotes] = useState(null);
  const [recipients, setRecipients] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api(`/api/meeting/${meeting.hash}`).then((data) => {
      if (data.error) return setError(data.error);
      setNotes(data);
      const saved = data.recipients && data.recipients.length ? data.recipients : state.recipients;
      setRecipients(saved.join(", "));
    });
    // Loaded once per meeting; the poll must not overwrite what is being typed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meeting.hash]);

  if (error) return <div className="doc">{head}<p className="bad">{error}</p></div>;
  if (!notes) return <div className="doc">{head}<p className="dim">loading…</p></div>;

  const set = (key) => (value) => setNotes({ ...notes, [key]: value });
  const setRow = (key, i, row) =>
    setNotes({ ...notes, [key]: notes[key].map((r, j) => (j === i ? row : r)) });
  const drop = (key, i) => setNotes({ ...notes, [key]: notes[key].filter((_, j) => j !== i) });

  // Empty rows are what "+ add" left behind; they are not part of the notes.
  function clean() {
    return {
      ...notes,
      participants: (notes.participants || []).filter(Boolean),
      action_items: (notes.action_items || []).filter((a) => a.owner.trim() || a.task.trim())
        .map((a) => ({ ...a, deadline: (a.deadline || "").trim() || null })),
      assumptions: (notes.assumptions || []).filter((a) => a.statement.trim()),
    };
  }

  const to = () => recipients.split(/[,\n]/).map((s) => s.trim()).filter(Boolean);

  async function save() {
    setBusy(true);
    const r = await api(`/api/meeting/${meeting.hash}`, { data: clean() });
    setBusy(false);
    say(r.ok ? "Saved" : r.error, r.ok ? "ok" : "bad");
  }

  async function send() {
    setBusy(true);
    const r = await api(`/api/meeting/${meeting.hash}/send`, { data: clean(), recipients: to() });
    setBusy(false);
    say(r.ok ? r.message : r.error, r.ok ? "ok" : "bad");
    refresh();
  }

  const actions = notes.action_items || [];
  const assumptions = notes.assumptions || [];

  return (
    <div className="doc-wrap">
      <div className="doc">
        {head}
        <Grow className="title" value={notes.title || ""} onChange={set("title")} placeholder="untitled" />
        <div className="byline">
          <input className="date" value={notes.date || ""} placeholder="date"
                 onChange={(e) => set("date")(e.target.value)} />
          <span className="dim">with</span>
          <input value={(notes.participants || []).join(", ")} placeholder="participants"
                 onChange={(e) => set("participants")(e.target.value.split(",").map((s) => s.trimStart()))} />
        </div>

        <Section label="summary">
          <Grow value={notes.summary || ""} onChange={set("summary")} placeholder="what happened" />
        </Section>

        <ListSection label="decisions" value={notes.decisions} onChange={set("decisions")} />

        <Section label="action items" extra={
          <button className="ghost" onClick={() => set("action_items")(
            [...actions, { owner: "", task: "", deadline: null }])}>+ add</button>}>
          {actions.length ? (
            <div className="table">
              {actions.map((a, i) => (
                <div className="tr" key={i}>
                  <input className="owner" placeholder="who" value={a.owner || ""}
                         onChange={(e) => setRow("action_items", i, { ...a, owner: e.target.value })} />
                  <Grow placeholder="what" value={a.task || ""}
                        onChange={(v) => setRow("action_items", i, { ...a, task: v })} />
                  <input className="due" placeholder="when" value={a.deadline || ""}
                         onChange={(e) => setRow("action_items", i, { ...a, deadline: e.target.value })} />
                  <button className="x" title="remove" onClick={() => drop("action_items", i)}>×</button>
                </div>
              ))}
            </div>
          ) : <p className="dim">none</p>}
        </Section>

        <ListSection label="open questions" value={notes.open_questions} onChange={set("open_questions")} />

        {assumptions.length > 0 && (
          <Section label={`assumptions · ${assumptions.length}`}>
            <p className="hint">Not said outright. Check the quote before relying on it.</p>
            {assumptions.map((s, i) => (
              <div className="assumption" key={i}>
                <div className="tr">
                  <Grow value={s.statement} onChange={(v) => setRow("assumptions", i, { ...s, statement: v })} />
                  <select value={s.confidence}
                          onChange={(e) => setRow("assumptions", i, { ...s, confidence: e.target.value })}>
                    {["low", "medium", "high"].map((l) => <option key={l}>{l}</option>)}
                  </select>
                  <button className="x" title="remove" onClick={() => drop("assumptions", i)}>×</button>
                </div>
                <blockquote>{s.evidence_quote}</blockquote>
              </div>
            ))}
          </Section>
        )}
      </div>

      <footer className="sendbar">
        <label className="to">
          <span>to</span>
          <input value={recipients} placeholder="name@company.com, …"
                 onChange={(e) => setRecipients(e.target.value)} />
        </label>
        <button disabled={busy} onClick={save}>save</button>
        <button className="primary" disabled={busy} onClick={send}
                title={meeting.status === "sent" ? "sent before — this sends it again" : ""}>
          {state.mail_mode === "smtp" ? "send" : "draft in outlook"}
        </button>
      </footer>
    </div>
  );
}

function Section({ label, extra, children }) {
  return (
    <section className="section">
      <h3>{label}{extra}</h3>
      {children}
    </section>
  );
}

// A list of strings edited as lines. The text is kept as typed, so a blank line
// being written is not swallowed; it is split only on the way out.
function ListSection({ label, value, onChange }) {
  const [text, setText] = useState((value || []).join("\n"));
  return (
    <Section label={label}>
      <Grow value={text} placeholder="one per line"
            onChange={(v) => { setText(v); onChange(lines(v)); }} />
    </Section>
  );
}

// A textarea as tall as what is in it.
function Grow({ value, onChange, className = "", placeholder }) {
  const ref = useRef(null);
  useLayoutEffect(() => {
    const el = ref.current;
    el.style.height = "auto";
    el.style.height = el.scrollHeight + "px";
  }, [value]);
  return (
    <textarea ref={ref} rows={1} className={`grow ${className}`} value={value}
              placeholder={placeholder} onChange={(e) => onChange(e.target.value)} />
  );
}
