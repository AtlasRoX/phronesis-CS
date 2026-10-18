"use client";

import React, { useEffect } from "react";
import { MarkdownRenderer } from "@/components/MarkdownRenderer";
import { useSmoothText } from "@/lib/useSmoothText";
import { ANSWER_SMOOTH_OPTIONS } from "@/lib/smoothText";
import type { CitationMap } from "@/types/citations";

interface SmoothMarkdownProps {
  /** Everything received so far (live) or the final answer. */
  content: string;
  /** True while the answer is still arriving. */
  streaming: boolean;
  /** Final citations: used once the reveal has caught up. */
  citations?: CitationMap;
  /** What was on screen before this mounted (hand-off from the live answer to the saved message). */
  initialText?: string;
  /** Called after each revealed update, e.g. to keep the view scrolled to the bottom. */
  onReveal?: (text: string) => void;
}

/**
 * Markdown revealed at a steady pace with a caret, ChatGPT/Claude style. Once the stream has ended and the
 * reveal has caught up, it renders exactly `content` through the regular (citation-aware) renderer.
 */
export function SmoothMarkdown({ content, streaming, citations, initialText, onReveal }: SmoothMarkdownProps) {
  const smooth = useSmoothText(content, streaming, ANSWER_SMOOTH_OPTIONS, { initialText });

  useEffect(() => {
    onReveal?.(smooth.text);
  }, [smooth.text, onReveal]);

  // Reduced motion: no reveal animation, but live text still renders through the streaming view (static caret).
  if (!streaming && !smooth.animating) {
    const final = <MarkdownRenderer content={content} citations={citations} />;
    // A hand-off whose final text was rewritten wholesale fades in rather than snapping.
    return smooth.rewrites ? <div className="smooth-crossfade">{final}</div> : final;
  }
  return (
    <div key={smooth.rewrites} className={`markdown-streaming${smooth.rewrites ? " smooth-crossfade" : ""}`}>
      <MarkdownRenderer content={smooth.text} streaming />
    </div>
  );
}
