"use client";

import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import {
  ANSWER_SMOOTH_OPTIONS,
  type Arrival,
  finishArrivals,
  pruneArrivals,
  reconcile,
  step,
  type SmoothTextOptions,
} from "@/lib/smoothText";

const REDUCED_MOTION_QUERY = "(prefers-reduced-motion: reduce)";

function subscribeReducedMotion(onChange: () => void): () => void {
  if (typeof window === "undefined" || !window.matchMedia) return () => {};
  const mql = window.matchMedia(REDUCED_MOTION_QUERY);
  mql.addEventListener("change", onChange);
  return () => mql.removeEventListener("change", onChange);
}

/** True when the reader asked the OS for reduced motion. */
export function usePrefersReducedMotion(): boolean {
  return useSyncExternalStore(
    subscribeReducedMotion,
    () => typeof window !== "undefined" && !!window.matchMedia && window.matchMedia(REDUCED_MOTION_QUERY).matches,
    () => false
  );
}

export interface UseSmoothTextConfig {
  /** Text already on screen when this mounts (e.g. the live answer being handed to the saved message). */
  initialText?: string;
  /** False shows `target` as is (e.g. a collapsed panel); nothing animates. */
  enabled?: boolean;
}

export interface SmoothText {
  /** The text to render now. */
  text: string;
  /** Increments on every full rewrite; key a wrapper on it to replay a short cross-fade. */
  rewrites: number;
  /** True while the reveal is still behind the target (or the stream is live). */
  animating: boolean;
}

/**
 * Reveals streamed text at a steady, readable cadence instead of in network bursts. Advances on
 * requestAnimationFrame, commits to React at ~30 fps, keeps the shared prefix on a revision, cross-fades a
 * full rewrite, and finishes the backlog within `finishMs` once the stream ends. With reduced motion (or
 * `enabled: false`) it returns the target unchanged.
 */
export function useSmoothText(
  target: string,
  streaming: boolean,
  options: SmoothTextOptions = ANSWER_SMOOTH_OPTIONS,
  config: UseSmoothTextConfig = {}
): SmoothText {
  const reducedMotion = usePrefersReducedMotion();
  const animate = (config.enabled ?? true) && !reducedMotion;

  const [view, setView] = useState<{ text: string; rewrites: number }>(() => {
    if (config.initialText !== undefined) {
      const r = reconcile(config.initialText, target, options);
      return { text: r.shown, rewrites: r.rewrite ? 1 : 0 };
    }
    return { text: streaming ? "" : target, rewrites: 0 };
  });

  const shownRef = useRef(view.text);
  const posRef = useRef(view.text.length);
  const rewritesRef = useRef(view.rewrites);
  const targetRef = useRef(target);
  const streamingRef = useRef(streaming);
  const optionsRef = useRef(options);
  const arrivalsRef = useRef<Arrival[]>([]);
  const finishingRef = useRef(false);
  const firstRunRef = useRef(true);
  const prevStreamingRef = useRef(streaming);
  const rafRef = useRef<number | null>(null);
  const lastFrameRef = useRef<number | null>(null);
  const lastCommitRef = useRef(0);
  const mountedRef = useRef(false);
  const committedRef = useRef(view.text);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    };
  }, []);

  useEffect(() => {
    const cancel = () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
      lastFrameRef.current = null;
    };
    const commit = (text: string) => {
      if (!mountedRef.current) return;
      committedRef.current = text;
      const rewrites = rewritesRef.current;
      setView((prev) => (prev.text === text && prev.rewrites === rewrites ? prev : { text, rewrites }));
    };

    const wasStreaming = prevStreamingRef.current;
    const firstRun = firstRunRef.current;
    prevStreamingRef.current = streaming;
    firstRunRef.current = false;
    targetRef.current = target;
    streamingRef.current = streaming;
    optionsRef.current = options;

    if (!animate) {
      cancel();
      arrivalsRef.current = [];
      finishingRef.current = false;
      // Rendered as is (see the return below); state catches up when animation is enabled again.
      shownRef.current = target;
      posRef.current = target.length;
      return;
    }

    const now = performance.now();
    let shown = shownRef.current;
    if (!target.startsWith(shown)) {
      const r = reconcile(shown, target, options);
      shown = r.shown;
      shownRef.current = shown;
      posRef.current = shown.length;
      arrivalsRef.current = [];
      if (r.rewrite) rewritesRef.current += 1;
      commit(shown);
    } else if (committedRef.current !== shown) {
      commit(shown);
    }

    if (streaming) {
      finishingRef.current = false;
      const last = arrivalsRef.current[arrivalsRef.current.length - 1];
      if (target.length > shown.length && (!last || last.end < target.length)) {
        arrivalsRef.current = [...arrivalsRef.current, { end: target.length, deadline: now + options.maxLagMs }];
      }
    } else if (shown !== target) {
      if (wasStreaming || firstRun || finishingRef.current) {
        // The stream just ended (or this is the hand-off from the live view): finish smoothly, quickly.
        finishingRef.current = true;
        arrivalsRef.current = finishArrivals(arrivalsRef.current, target.length, now, options);
      } else {
        // A settled text changed outside a stream: nothing to animate.
        cancel();
        shownRef.current = target;
        posRef.current = target.length;
        arrivalsRef.current = [];
        commit(target);
        return;
      }
    }

    if (shownRef.current === target || rafRef.current !== null) return;

    const tick = (ts: number) => {
      rafRef.current = null;
      if (!mountedRef.current) return;
      const tgt = targetRef.current;
      const opts = optionsRef.current;
      const dt = lastFrameRef.current === null ? 16 : ts - lastFrameRef.current;
      lastFrameRef.current = ts;
      const next = step(
        tgt,
        { shownLength: shownRef.current.length, pos: posRef.current },
        { streaming: streamingRef.current, dtMs: dt, now: ts, arrivals: arrivalsRef.current },
        opts
      );
      posRef.current = next.pos;
      if (next.shownLength !== shownRef.current.length) {
        shownRef.current = tgt.slice(0, next.shownLength);
        arrivalsRef.current = pruneArrivals(arrivalsRef.current, next.shownLength);
      }
      const caughtUp = shownRef.current === tgt;
      // Commit at ~30 fps; the last step is committed at once so the final text is exact.
      if (caughtUp || ts - lastCommitRef.current >= opts.commitIntervalMs) {
        lastCommitRef.current = ts;
        commit(shownRef.current);
      }
      if (caughtUp) {
        lastFrameRef.current = null;
        if (!streamingRef.current) finishingRef.current = false;
        return;
      }
      // While streaming, a held-back word fragment keeps the loop alive until its deadline passes.
      rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);
  }, [target, streaming, animate, options]);

  if (!animate) return { text: target, rewrites: view.rewrites, animating: false };
  return { text: view.text, rewrites: view.rewrites, animating: streaming || view.text !== target };
}
