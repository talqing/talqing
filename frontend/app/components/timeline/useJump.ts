"use client";
import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Scroll to a turn or a beat, and mark it on arrival.
 *
 * Several turns fit on one screen, so scrolling alone leaves the reader hunting
 * for the one they asked for. The mark wears the playhead highlight's colour,
 * because both answer "which one is it".
 */
export function useJump(): { flashId: string | null; jumpTo: (id: string) => void } {
  const [flashId, setFlashId] = useState<string | null>(null);
  const timer = useRef<number | null>(null);
  const jumpTo = useCallback((id: string) => {
    document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
    setFlashId(id);
    if (timer.current !== null) window.clearTimeout(timer.current);
    // Long enough to survive the smooth scroll and still be there when the turn
    // arrives; short enough that it does not become a second selection state.
    timer.current = window.setTimeout(() => setFlashId(null), 2200);
  }, []);
  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    [],
  );
  return { flashId, jumpTo };
}
