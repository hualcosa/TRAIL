/**
 * Two media-query hooks, both of which exist because a CSS-only answer would
 * not have been enough.
 *
 * `prefers-reduced-motion` is honoured in the stylesheet as well, but the
 * typewriter reveal is a JavaScript timer rather than a transition — CSS cannot
 * switch it off, so the component has to ask. The 900px breakpoint is likewise
 * a structural change and not a styling one: below it the ficha becomes a
 * collapsed strip with a real toggle button and `aria-expanded`, and a control
 * that exists in the accessibility tree only at some viewport widths has to be
 * rendered conditionally, not hidden with `display: none`.
 */

import { useEffect, useState } from "react";

function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState<boolean>(() =>
    typeof window === "undefined" ? false : window.matchMedia(query).matches,
  );

  useEffect(() => {
    const list = window.matchMedia(query);
    const onChange = (event: MediaQueryListEvent) => setMatches(event.matches);
    // Re-read on subscribe: the query can have flipped between the initial
    // render and this effect, and a stale `false` here is a permanently
    // animating page for someone who asked for no animation.
    setMatches(list.matches);
    list.addEventListener("change", onChange);
    return () => list.removeEventListener("change", onChange);
  }, [query]);

  return matches;
}

export function useReducedMotion(): boolean {
  return useMediaQuery("(prefers-reduced-motion: reduce)");
}

export function useNarrowViewport(): boolean {
  return useMediaQuery("(max-width: 899px)");
}
