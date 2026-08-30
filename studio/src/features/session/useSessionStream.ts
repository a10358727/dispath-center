import { useEffect, useRef, useState } from "react";
import { websocketUrl } from "@/api/client";
import type { SessionEvent } from "@/api/types";

/** One WebSocket per open session: the server replays the persisted log
 *  after `after_seq` and then follows live, so a reconnect never loses an
 *  event (SQLite is the truth; this hook is a disposable cache). */
export function useSessionStream(sessionId: string | undefined) {
  const [events, setEvents] = useState<SessionEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const lastSeq = useRef(0);

  useEffect(() => {
    setEvents([]);
    lastSeq.current = 0;
    if (!sessionId) return;
    let socket: WebSocket | null = null;
    let closed = false;
    let attempt = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const connect = () => {
      if (closed) return;
      socket = new WebSocket(
        websocketUrl(`/api/v2/studio/sessions/${encodeURIComponent(sessionId)}/stream?after_seq=${lastSeq.current}`),
      );
      socket.onopen = () => {
        attempt = 0;
        setConnected(true);
      };
      socket.onmessage = (message) => {
        let event: SessionEvent;
        try {
          event = JSON.parse(String(message.data)) as SessionEvent;
        } catch {
          return;
        }
        if (typeof event.seq !== "number" || event.seq <= lastSeq.current) return;
        lastSeq.current = event.seq;
        setEvents((previous) => [...previous, event]);
      };
      socket.onclose = () => {
        setConnected(false);
        if (closed) return;
        attempt += 1;
        timer = setTimeout(connect, Math.min(10_000, 1_000 * attempt));
      };
      socket.onerror = () => socket?.close();
    };
    connect();
    return () => {
      closed = true;
      if (timer) clearTimeout(timer);
      socket?.close();
    };
  }, [sessionId]);

  return { events, connected };
}
