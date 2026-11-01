import { useEffect, useMemo, useRef, useState } from "react";
import { MARK, api, day } from "./api.js";
import Notes from "./Notes.jsx";

// Everything the list can hold, as one shape: a meeting, a recording that never
// became one (nothing was recognised in it), or something in the archive.
function items(state) {
  const meetings = state.meetings.map((m) => ({
    key: m.hash, kind: "meeting", title: m.title, date: m.date, status: m.status, meeting: m }));
  const recordings = state.recordings.map((r) => ({
    key: `rec:${r.name}`, kind: "recording", title: r.title, date: r.date,
    status: r.status ? "transcribing" : "recorded", recording: r }));
  return [...meetings, ...recordings].sort((a, b) => (b.date || "").localeCompare(a.date || ""));
}

function archived(state) {
  return state.archive.map((e) => ({
    key: `arc:${e.id}`, kind: "archived", title: e.title,
    date: new Date(e.archived_at * 1000).toISOString().slice(0, 10), status: "archived", entry: e }));
}

const FILTERS = [
  ["all", () => true],
  ["to send", (i) => i.status === "ready"],
  ["sent", (i) => i.status === "sent"],
  ["busy", (i) => ["found", "processing", "transcribing"].includes(i.status)],
  ["failed", (i) => i.status === "failed" || i.status === "recorded"],
];

const WORDS = { found: "queued", processing: "summarising", ready: "to send", sent: "sent",
                failed: "failed", recorded: "nothing recognised", transcribing: "transcribing",
                archived: "deleted" };
const MARKS = { ...MARK, recorded: "◌", transcribing: "…", archived: "⌫" };

export default function Meetings({ state, selected, onSelect, refresh, say }) {
  const [filter, setFilter] = useState("all");
  const [query, setQuery] = useState("");
  const [menu, setMenu] = useState(null);       // {item, x, y}

  function openMenu(item, e) {
    e.preventDefault();
    onSelect(item.key);
    setMenu({ item, x: e.clientX, y: e.clientY });
  }

  const all = useMemo(() => items(state), [state]);
  const shown = useMemo(() => {
    const pool = filter === "archive" ? archived(state) : all;
    // The archive is not one of the filters: it is a different list altogether.
    const test = filter === "archive" ? () => true : FILTERS.find(([name]) => name === filter)[1];
    const q = query.trim().toLowerCase();
    return pool.filter((i) => test(i) && (!q || i.title.toLowerCase().includes(q)));
  }, [all, state, filter, query]);

  // Nothing chosen yet: the newest meeting waiting to be sent, else the newest.
  useEffect(() => {
    if (!selected && all.length) {
      onSelect((all.find((i) => i.status === "ready") || all[0]).key);
    }
  }, [selected, all, onSelect]);

  const item = [...all, ...archived(state)].find((i) => i.key === selected);

  return (
    <div className="split">
      <section className="list">
        <div className="list-head">
          <input className="search" placeholder="search" value={query}
                 onChange={(e) => setQuery(e.target.value)} />
          <div className="filters">
            {filter === "archive" && <span className="archive-title">archive · deleted for good after {state.archive_days} days</span>}
            {filter !== "archive" && FILTERS.map(([name]) => (
              <button key={name} className={filter === name ? "on" : ""}
                      onClick={() => setFilter(name)}>{name}</button>
            ))}
          </div>
        </div>
        <ul>
          {shown.map((i) => (
            <li key={i.key} className={i.key === selected ? "on" : ""} onClick={() => onSelect(i.key)}
                onDoubleClick={(e) => openMenu(i, e)} onContextMenu={(e) => openMenu(i, e)}>
              <span className="title">{i.title}</span>
              <span className={`meta ${i.status}`}>
                {MARKS[i.status] || "·"} {WORDS[i.status] || i.status}
                {(i.meeting?.recorded || i.kind === "recording") && <span className="rec">rec</span>}
                <span className="date">{day(i.date)}</span>
              </span>
            </li>
          ))}
          {!shown.length && (
            <li className="empty">
              {filter === "archive" ? "the archive is empty" : all.length ? "nothing matches" : "nothing yet"}
            </li>
          )}
        </ul>
        <button className={`archive-toggle ${filter === "archive" ? "on" : ""}`}
                onClick={() => setFilter(filter === "archive" ? "all" : "archive")}>
          {filter === "archive" ? "← back to meetings" : `⌫ archive · ${state.archive.length}`}
        </button>
      </section>

      <section className="detail">
        {item?.kind === "meeting" && (
          <Detail key={item.key + item.status} meeting={item.meeting} state={state}
                  refresh={refresh} say={say} more={(e) => openMenu(item, e)} />
        )}
        {item?.kind === "recording" && (
          <LoneRecording key={item.key} recording={item.recording} more={(e) => openMenu(item, e)} />
        )}
        {item?.kind === "archived" && (
          <Archived key={item.key} entry={item.entry} days={state.archive_days}
                    more={(e) => openMenu(item, e)} />
        )}
        {!item && (
          <div className="nothing">
            <pre>{CLOUD}</pre>
            {all.length ? "pick a meeting"
              : <span>no meetings yet<br />record a call, process a file,<br />or drop a Teams transcript into the watched folder</span>}
          </div>
        )}
      </section>
      {menu && (
        <Menu at={menu} close={() => setMenu(null)}
              actions={actionsFor(menu.item, state, say, refresh, () => onSelect(null))} />
      )}
    </div>
  );
}

// What can be done to an item, in the order the menu shows it.
function actionsFor(item, state, say, refresh, gone) {
  if (item.kind === "archived") {
    return [{ label: "restore", run: () => act(`/api/archive/${encodeURIComponent(item.entry.id)}/restore`,
                                              say, refresh, () => { say("Restored", "ok"); gone(); }) }];
  }
  if (item.kind === "recording") {
    const base = `/api/recording/${encodeURIComponent(item.recording.name)}`;
    if (item.recording.status) return [];
    return [
      { label: "transcribe again", run: () => act(`${base}/transcribe`, say, refresh,
                                                  () => say("Transcribing the whole recording", "ok")) },
      { label: "delete", danger: true, confirm: true,
        run: () => act(`${base}/archive`, say, refresh, gone) },
    ];
  }
  const m = item.meeting, base = `/api/meeting/${m.hash}`;
  return [
    { label: "show in finder", run: () => act(`${base}/reveal`, say, refresh) },
    ...(m.recorded ? [{ label: "transcribe again", hint: "the whole recording, with the speech model",
                        run: () => act(`${base}/transcribe`, say, refresh,
                                       () => say("Transcribing the whole recording again", "ok")) }] : []),
    { label: "summarise again", run: async () => {
      const r = await api(`${base}/retry`, {});
      say(r.error || (r.queued ? "Queued again" : r.note), r.error ? "bad" : "ok");
      refresh();
    } },
    { label: "delete", danger: true, confirm: true,
      run: () => act(`${base}/archive`, say, refresh, () => {
        say(`Moved to the archive for ${state.archive_days} days`, "ok");
        gone();
      }) },
  ];
}

// A small menu where it was asked for. Delete asks once more inside it.
function Menu({ at, actions, close }) {
  const [armed, setArmed] = useState(null);
  const box = useRef(null);
  useEffect(() => {
    const away = (e) => { if (!box.current?.contains(e.target)) close(); };
    const key = (e) => { if (e.key === "Escape") close(); };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", key);
    };
  }, [close]);
  // Kept inside the window, whichever corner it was opened near.
  const x = Math.min(at.x, window.innerWidth - 210), y = Math.min(at.y, window.innerHeight - 40 * actions.length - 20);
  if (!actions.length) return null;
  return (
    <div className="glass menu" ref={box} style={{ left: x, top: y }}>
      {actions.map((a, i) => (
        <button key={a.label} className={a.danger ? "danger" : ""} title={a.hint || ""}
                onClick={() => {
                  if (a.confirm && armed !== i) return setArmed(i);
                  close();
                  a.run();
                }}>
          {a.confirm && armed === i ? `${a.label} — sure?` : a.label}
        </button>
      ))}
    </div>
  );
}

function More({ onClick }) {
  return <button className="ghost more" title="More — or double-click the meeting in the list"
                 onClick={(e) => { const r = e.currentTarget.getBoundingClientRect();
                                   onClick({ preventDefault() {}, clientX: r.right - 190, clientY: r.bottom + 4 }); }}>⋯</button>;
}

async function act(path, say, refresh, done) {
  const r = await api(path, {});
  if (r.ok === false || r.error) say(r.error, "bad");
  else if (done) done();
  refresh();
  return r;
}

function Detail({ meeting, state, refresh, say, more }) {
  const [tab, setTab] = useState(meeting.notes ? "notes" : "transcript");

  const head = (
    <header className="detail-head">
      <span className={`chip ${meeting.status}`}>{MARK[meeting.status]} {WORDS[meeting.status]}</span>
      <div className="tabs">
        {["notes", "transcript"].map((t) => (
          <button key={t} className={tab === t ? "on" : ""} onClick={() => setTab(t)}>{t}</button>
        ))}
      </div>
      <span className="spacer" />
      <More onClick={more} />
    </header>
  );

  if (tab === "transcript") return <Transcript meeting={meeting} head={head} />;

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

function LoneRecording({ recording, more }) {
  const busy = !!recording.status;
  return (
    <div className="doc">
      <header className="detail-head">
        <span className="chip">{busy ? "… transcribing" : "◌ nothing recognised"}</span>
        <span className="spacer" />
        {!busy && <More onClick={more} />}
      </header>
      <h1>{recording.title}</h1>
      <p className="dim">
        {busy ? "The speech model is going through the whole recording. The meeting appears in the list when it is done."
              : "The live transcript found no speech in this recording, so it was not summarised. "
                + "Transcribe it again from ⋯ to run the speech model over the whole recording, or delete it."}
      </p>
    </div>
  );
}

function Archived({ entry, days, more }) {
  const left = Math.max(0, Math.ceil((entry.deletes_at * 1000 - Date.now()) / 86400000));
  return (
    <div className="doc">
      <header className="detail-head">
        <span className="chip">⌫ deleted</span>
        <span className="spacer" />
        <More onClick={more} />
      </header>
      <h1>{entry.title}</h1>
      <p className="dim">
        In the archive since {new Date(entry.archived_at * 1000).toLocaleDateString("en-GB")}.
        {" "}Removed for good in {left} day{left === 1 ? "" : "s"} (the archive keeps things {days} days;
        change it in settings). Restore, from ⋯, puts everything back where it was.
      </p>
    </div>
  );
}

const CLOUD = `   .--.
.-(    ).
(___.__)_)`;

// What was said, read again from the source: a downloaded file or a recording.
// The voices can be named here; a name goes into the notes as well.
function Transcript({ meeting, head }) {
  const [data, setData] = useState(null);
  const load = () => api(`/api/meeting/${meeting.hash}/transcript`).then(setData);
  useEffect(() => { load(); }, [meeting.hash]);   // eslint-disable-line react-hooks/exhaustive-deps

  const voices = data && !data.error
    ? [...new Set(data.turns.map((t) => t.speaker))].filter((s) => s !== "Me" && s !== "Unknown")
    : [];

  return (
    <div className="doc">
      {head}
      {!data && <p className="dim">loading…</p>}
      {data?.error && <p className="bad">{data.error}</p>}
      {meeting.notes && voices.length > 0 && <Voices meeting={meeting} voices={voices} onNamed={load} />}
      {data && !data.error && (
        <div className="transcript">
          {data.turns.map((t, i) => (
            <div key={i} className={`said ${t.speaker === "Me" ? "me" : "them"}`}>
              <span className="who" title={t.speaker}>{t.speaker === "Unknown" ? "" : t.speaker}</span>
              <p>{t.text}</p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function Voices({ meeting, voices, onNamed }) {
  const [names, setNames] = useState({});
  const [note, setNote] = useState("");

  async function save(voice) {
    const to = (names[voice] || "").trim();
    if (!to || to === voice) return;
    const r = await api(`/api/meeting/${meeting.hash}/speaker`, { from: voice, to });
    setNote(r.ok ? `${voice} is ${to} now — in the notes too` : r.error);
    setNames({ ...names, [voice]: "" });
    onNamed();
  }

  return (
    <section className="voices">
      <h3>who is who</h3>
      {voices.map((voice) => (
        <label key={voice} className="voice">
          <span>{voice}</span>
          <input placeholder="name" value={names[voice] || ""}
                 onChange={(e) => setNames({ ...names, [voice]: e.target.value })}
                 onKeyDown={(e) => e.key === "Enter" && save(voice)}
                 onBlur={() => save(voice)} />
        </label>
      ))}
      {note && <p className="hint">{note}</p>}
    </section>
  );
}
