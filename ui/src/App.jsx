import { useCallback, useEffect, useRef, useState } from "react";
import { api, bar, jobLine, shortPath } from "./api.js";
import Live, { clock, useElapsed } from "./Live.jsx";
import Meetings from "./Meetings.jsx";
import Settings from "./Settings.jsx";
import Sky from "./Sky.jsx";

const POLL_MS = 2000;

export default function App() {
  const [state, setState] = useState(null);
  const [view, setView] = useState("meetings");
  const [selected, setSelected] = useState(
    new URLSearchParams(location.search).get("meeting"));
  const [notice, setNotice] = useState(null);

  const refresh = useCallback(async () => {
    try {
      setState(await api("/api/state"));
    } catch {
      /* the app is quitting, or restarting the server; the next poll will tell */
    }
  }, []);

  // Pressing record — here or in the menu bar — brings up the live view.
  const wasRecording = useRef(false);
  useEffect(() => {
    const now = !!state?.recording?.recording;
    if (now && !wasRecording.current) setView("live");
    wasRecording.current = now;
  }, [state?.recording?.recording]);

  useEffect(() => {
    refresh();
    const timer = setInterval(() => document.hidden || refresh(), POLL_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  // The menu bar's "Open notes…" lands here when the window is already up.
  useEffect(() => {
    window.openMeeting = (meeting) => {
      setView("meetings");
      setSelected(meeting);
      refresh();
    };
    return () => delete window.openMeeting;
  }, [refresh]);

  const say = useCallback((text, kind = "", lines = []) =>
    setNotice({ text, kind, lines, at: Date.now() }), []);

  // Files brought in by hand — picked, dropped on the window, re-checked — come
  // back through the state, the drop from the native side, so one toast for all.
  const lastImport = useRef(undefined);
  useEffect(() => {
    if (!state) return;
    const at = state.imported?.at ?? null;
    if (lastImport.current !== undefined && at !== lastImport.current) {
      say(...importNotice(state.imported.results));
    }
    lastImport.current = at;
  }, [state, say]);

  async function pick(route) {
    const r = await api(route, {});
    if (r.cancelled) return;
    if (r.error) say(r.error, "bad");
    // One file: show it, whether it is new or summarised before.
    if (r.results?.length === 1 && r.results[0].meeting) {
      setView("meetings");
      setSelected(r.results[0].meeting);
    }
    refresh();
  }

  return (
    <>
      <Sky />
      {state && (
        <div className="shell">
          <div className="glass frame">
            <aside className="rail">
              <pre className="brand">{"▞▚ meeting\n▚▞ notes"}</pre>

              <nav>
                {(state.recording.recording || state.recording.transcribing || view === "live") && (
                  <button className={`live-nav ${view === "live" ? "on" : ""}`} onClick={() => setView("live")}>
                    {state.recording.recording ? "● live" : "last call"}
                  </button>
                )}
                <button className={view === "meetings" ? "on" : ""} onClick={() => setView("meetings")}>
                  meetings<span className="count">{state.meetings.length || ""}</span>
                </button>
                <button className={view === "archived" ? "on" : ""} onClick={() => setView("archived")}>
                  archived<span className="count">{state.archive.length || ""}</span>
                </button>
                <button className={view === "settings" ? "on" : ""} onClick={() => setView("settings")}>
                  settings
                </button>
              </nav>

              <div className="pick">
                <button className="primary" onClick={() => pick("/api/pick")}>process files…</button>
                <button className="link" onClick={() => pick("/api/pick/folder")}>or a whole folder…</button>
              </div>
              <Record recording={state.recording} call={state.call} refresh={refresh} say={say}
                      onStart={() => setView("live")} />

              <Status state={state} />
            </aside>

            <main className="card">
              {(view === "meetings" || view === "archived") && (
                <Meetings key={view} state={state} archive={view === "archived"} selected={selected}
                          onSelect={setSelected} refresh={refresh} say={say} />
              )}
              {view === "live" && <Live recording={state.recording} refresh={refresh} say={say} />}
              {view === "settings" && <Settings state={state} refresh={refresh} say={say} />}
            </main>
          </div>
        </div>
      )}
      {notice && <Toast key={notice.at} notice={notice} onDone={() => setNotice(null)} />}
    </>
  );
}

// The call recorder's button. The clock and the meters live in the live view.
// When a call app takes or lets go of the microphone, the offer sits above it.
const OFFERS = { record: "is using the microphone", stop: "let go of the microphone — call over?" };

function Record({ recording, call, refresh, say, onStart }) {
  const seconds = useElapsed(recording);
  async function toggle() {
    const starting = !recording.recording;
    const r = await api("/api/record", { on: starting });
    if (r.error) say(r.error, "bad");
    else if (starting) onStart();
    refresh();
  }
  async function dismiss() {
    await api("/api/call/dismiss", {});
    refresh();
  }
  return (
    <div className={`record ${recording.recording ? "on" : ""}`}>
      {call.offer && (
        <p className="offer">
          <b>{call.app.toLowerCase()}</b> {OFFERS[call.offer]}
          <button className="link" onClick={dismiss}>{call.offer === "stop" ? "keep going" : "not now"}</button>
        </p>
      )}
      <button className={call.offer ? "offered" : ""} onClick={toggle}>
        {recording.recording
          ? <>■ stop <span className="clock">{clock(seconds)}</span></>
          : call.offer === "record" ? `● record the ${call.app.toLowerCase()} call` : "● record the call"}
      </button>
    </div>
  );
}

function Status({ state }) {
  const job = state.job;
  return (
    <dl className="status">
      <dt>ollama</dt>
      <dd className={state.ollama_installed ? "" : "bad"}
          title={state.ollama_up || !state.ollama_installed ? "" : "started when a meeting needs it"}>
        {state.ollama_up ? "running" : state.ollama_installed ? "asleep" : "not installed"}
      </dd>
      <dt>model</dt>
      <dd title={state.model_ready ? "" : "not downloaded yet"}>
        {state.model || "—"}{!state.model_ready && <span className="dim"> ↓</span>}
      </dd>
      <dt>watch</dt>
      <dd title={state.folder}>{state.watching ? shortPath(state.folder) : "paused"}</dd>
      <dt>now</dt>
      <dd>{jobLine(job)}</dd>
      {job.active && (
        <dd className="job">
          <span className="file" title={job.file}>{job.file}</span>
          <pre className={job.n ? "" : "pulse"}>
            {job.n ? `${bar(job.i, job.n, 12)} ${job.i}/${job.n}` : "[" + "▒".repeat(12) + "]"}
          </pre>
        </dd>
      )}
    </dl>
  );
}

const OUTCOMES = [["queued", "queued"], ["seen", "already here"],
                  ["skipped", "not transcripts"], ["error", "unreadable"]];

// "2 queued · 1 not transcripts", and below it what was not taken and why.
export function importNotice(results) {
  if (!results.length) return ["No transcripts there", ""];
  const count = (o) => results.filter((r) => r.outcome === o).length;
  const text = OUTCOMES.filter(([o]) => count(o)).map(([o, word]) => `${count(o)} ${word}`).join(" · ");
  const why = results.filter((r) => r.outcome === "skipped" || r.outcome === "error")
    .map((r) => `${r.name}: ${r.note}`);
  const lines = why.length > 6 ? [...why.slice(0, 5), `and ${why.length - 5} more`] : why;
  return [text, count("queued") ? "ok" : count("error") === results.length ? "bad" : "", lines];
}

function Toast({ notice, onDone }) {
  // Once per toast: onDone is a new function on every poll, and a timer
  // restarted every two seconds would never fire.
  useEffect(() => {
    const timer = setTimeout(onDone, 6000);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return (
    <div className={`glass toast ${notice.kind}`} onClick={onDone}>
      {notice.text}
      {notice.lines?.map((line) => <div key={line} className="why">{line}</div>)}
    </div>
  );
}
