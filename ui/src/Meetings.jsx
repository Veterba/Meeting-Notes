import { useEffect, useMemo, useState } from "react";
import { MARK, api, day } from "./api.js";
import Notes from "./Notes.jsx";

const FILTERS = [
  ["all", () => true],
  ["to send", (m) => m.status === "ready"],
  ["sent", (m) => m.status === "sent"],
  ["busy", (m) => m.status === "found" || m.status === "processing"],
  ["failed", (m) => m.status === "failed"],
];

const WORDS = { found: "queued", processing: "summarising", ready: "to send",
                sent: "sent", failed: "failed" };

export default function Meetings({ state, selected, onSelect, refresh, say }) {
  const [filter, setFilter] = useState("all");
  const [query, setQuery] = useState("");

  const shown = useMemo(() => {
    const test = FILTERS.find(([name]) => name === filter)[1];
    const q = query.trim().toLowerCase();
    return state.meetings.filter((m) => test(m) && (!q || m.title.toLowerCase().includes(q)));
  }, [state.meetings, filter, query]);

  // Nothing chosen yet: the newest meeting waiting to be sent, else the newest.
  useEffect(() => {
    if (!selected && state.meetings.length) {
      const first = state.meetings.find((m) => m.status === "ready") || state.meetings[0];
      onSelect(first.hash);
    }
  }, [selected, state.meetings, onSelect]);

  const meeting = state.meetings.find((m) => m.hash === selected);

  return (
    <div className="split">
      <section className="list">
        <div className="list-head">
          <input className="search" placeholder="search" value={query}
                 onChange={(e) => setQuery(e.target.value)} />
          <div className="filters">
            {FILTERS.map(([name]) => (
              <button key={name} className={filter === name ? "on" : ""}
                      onClick={() => setFilter(name)}>{name}</button>
            ))}
          </div>
        </div>
        <ul>
          {shown.map((m) => (
            <li key={m.hash} className={m.hash === selected ? "on" : ""}
                onClick={() => onSelect(m.hash)}>
              <span className="title">{m.title}</span>
              <span className={`meta ${m.status}`}>
                {MARK[m.status] || "·"} {WORDS[m.status] || m.status}
                <span className="date">{day(m.date)}</span>
              </span>
            </li>
          ))}
          {!shown.length && (
            <li className="empty">
              {state.meetings.length ? "nothing matches" : "nothing yet"}
            </li>
          )}
        </ul>
      </section>

      <section className="detail">
        {meeting
          ? <Detail key={meeting.hash + meeting.status} meeting={meeting} state={state}
                    refresh={refresh} say={say} />
          : <div className="nothing">
              <pre>{CLOUD}</pre>
              {state.meetings.length ? "pick a meeting"
                : <span>no meetings yet<br />process a file, or drop a Teams<br />transcript into the watched folder</span>}
            </div>}
      </section>
    </div>
  );
}

function Detail({ meeting, state, refresh, say }) {
  async function retry() {
    const r = await api(`/api/meeting/${meeting.hash}/retry`, {});
    say(r.error || (r.queued ? "Queued again" : r.note), r.error ? "bad" : "ok");
    refresh();
  }

  async function reveal() {
    const r = await api(`/api/meeting/${meeting.hash}/reveal`, {});
    if (r.error) say(r.error, "bad");
  }

  const head = (
    <header className="detail-head">
      <span className={`chip ${meeting.status}`}>{MARK[meeting.status]} {WORDS[meeting.status]}</span>
      <span className="spacer" />
      <button className="ghost" onClick={reveal}>transcript</button>
      <button className="ghost" onClick={retry}>reprocess</button>
    </header>
  );

  if (!meeting.notes) {
    const working = meeting.status === "found" || meeting.status === "processing";
    return (
      <div className="doc">
        {head}
        <h1>{meeting.title}</h1>
        <p className={working ? "dim" : "bad"}>
          {working ? "Being summarised. The notes appear here when they are ready."
                   : meeting.error || "This meeting has no notes."}
        </p>
      </div>
    );
  }
  return <Notes meeting={meeting} state={state} head={head} refresh={refresh} say={say} />;
}

const CLOUD = `   .--.
.-(    ).
(___.__)_)`;
