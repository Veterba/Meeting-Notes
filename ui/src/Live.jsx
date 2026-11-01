import { useEffect, useRef, useState } from "react";
import { api } from "./api.js";

// The call as it happens: what each side said, a few seconds behind.
export default function Live({ recording, refresh, say }) {
  const [lines, setLines] = useState([]);
  const [partial, setPartial] = useState([]);   // what is being said right now
  const [status, setStatus] = useState({ status: "", error: "" });
  const box = useRef(null);
  const pinned = useRef(true);             // follow new lines unless scrolled up

  useEffect(() => {
    let version = -1, gone = false;
    async function poll() {
      try {
        const r = await api(`/api/live?version=${version}`);
        if (gone) return;
        if (r.lines) setLines(r.lines);      // only sent when they changed
        if (r.partial) setPartial(r.partial);
        version = r.version;
        setStatus({ status: r.status, error: r.error });
      } catch { /* the next poll will tell */ }
    }
    poll();
    const timer = setInterval(poll, 1000);
    return () => { gone = true; clearInterval(timer); };
  }, []);

  useEffect(() => {
    const el = box.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [lines, partial]);

  async function stop() {
    const r = await api("/api/record", { on: false });
    if (r.error) say(r.error, "bad");
    refresh();
  }

  const on = recording.recording;
  const s = useElapsed(recording);
  const note = status.error ? status.error
    : !on && recording.transcribing
      ? (status.status && !["listening", "done"].includes(status.status) ? `${status.status}…`
                                                                         : "transcribing the last words…")
    : !on ? "saved — the notes are being written, they will appear under meetings"
    : status.status === "listening" ? (lines.length ? "" : "listening — the first words appear a few seconds after they are said")
    : status.status;

  return (
    <div className="live">
      <header className="live-head">
        <span className={`dot ${on ? "on" : ""}`} />
        <span className="clock">{clock(s)}</span>
        {on && (
          <pre className="meters">{`me ${meter(recording.mic)}   them ${meter(recording.system)}`}</pre>
        )}
        <span className="spacer" />
        {recording.warning && on && <span className="bad small">{recording.warning}</span>}
        {on && <button className="stop" onClick={stop}>■ stop</button>}
      </header>

      <div className="live-lines" ref={box}
           onScroll={(e) => {
             const el = e.currentTarget;
             pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
           }}>
        {lines.map((line, i) => (
          <div key={i} className={`said ${line.who}`}>
            <span className="at">{clock(Math.floor(line.start))}</span>
            <span className="who">{line.who}</span>
            <p>{line.text}</p>
          </div>
        ))}
        {partial.map((line) => (
          <div key={`p-${line.who}`} className={`said ${line.who} partial`}>
            <span className="at">{clock(Math.floor(line.start))}</span>
            <span className="who">{line.who}</span>
            <p>{line.text}<span className="caret">▍</span></p>
          </div>
        ))}
        {note && <p className={`live-note ${status.error ? "bad" : ""}`}>{note}</p>}
      </div>
    </div>
  );
}

// Seconds since the recording started, ticking here rather than with the poll:
// the state arrives every two seconds, a clock has to move every one.
export function useElapsed(recording) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!recording.recording) return;
    const timer = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(timer);
  }, [recording.recording]);
  return recording.started_at ? Math.max(0, Math.floor(now / 1000 - recording.started_at))
                              : Math.floor(recording.seconds || 0);
}

export function clock(s) {
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

export function meter(level, width = 8) {
  // Peaks are 0..1; speech sits low, so the scale is a square root.
  const n = Math.round(Math.sqrt(Math.min(1, level || 0)) * width);
  return "▮".repeat(n) + "▯".repeat(width - n);
}
