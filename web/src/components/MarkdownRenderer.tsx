"use client";

import React, { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown, { type Components, type Options as MarkdownOptions } from "react-markdown";
import remarkGfm from "remark-gfm";
import { Check, Copy, ExternalLink, Terminal } from "lucide-react";
import { CitationRef } from "@/components/CitationRef";
import { rehypeCitations, rehypeIdentifierLinkGuard } from "@/lib/citations";
import {
  rehypeStripUnsafe,
  remarkHtmlBreaks,
  safeUrlTransform,
  splitStreamingBlocks,
  stabilizeStreamingTail,
} from "@/lib/markdownSafety";
import type { CitationMap } from "@/types/citations";

interface MarkdownRendererProps {
  content: string;
  /** Identifiers cited in the answer, linked to their sources; omitted while streaming. */
  citations?: CitationMap;
  /** Live text: finished blocks are memoized so each new token re-parses only the last block. */
  streaming?: boolean;
}

type HastNode = {
  type: string;
  tagName?: string;
  value?: string;
  properties?: Record<string, unknown>;
  children?: HastNode[];
};

function hastText(node: HastNode): string {
  if (node.type === "text") return node.value || "";
  return (node.children || []).map(hastText).join("");
}

function hastFind(node: HastNode, tag: string): HastNode[] {
  const out: HastNode[] = [];
  for (const child of node.children || []) {
    if (child.type !== "element") continue;
    if (child.tagName === tag) out.push(child);
    if (child.tagName !== "table") out.push(...hastFind(child, tag));
  }
  return out;
}

/**
 * Label every cell with its column header, and mark tables whose cells hold paragraphs.
 * Those are unreadable squeezed into narrow columns, so they render as stacked rows instead.
 */
function rehypeReadableTables() {
  const annotate = (table: HastNode) => {
    const headers = hastFind(table, "th").map((th) => hastText(th).trim());
    const lengths: number[] = [];
    for (const tr of hastFind(table, "tr")) {
      hastFind(tr, "td").forEach((td, i) => {
        const length = hastText(td).trim().length;
        // Short values (rank, score, tier) sit side by side when the table is stacked.
        td.properties = { ...td.properties, dataLabel: headers[i] || "", ...(length <= 40 ? { dataShort: "" } : {}) };
        lengths.push(length);
      });
    }
    if (!lengths.length) return;
    const longest = Math.max(...lengths);
    const average = lengths.reduce((a, b) => a + b, 0) / lengths.length;
    if (headers.length >= 4 && (longest > 120 || average > 50)) {
      table.properties = { ...table.properties, dataLayout: "stacked" };
    } else if (headers.length >= 6) {
      table.properties = { ...table.properties, dataLayout: "dense" };
    }
  };
  const visit = (node: HastNode) => {
    if (node.type === "element" && node.tagName === "table") annotate(node);
    (node.children || []).forEach(visit);
  };
  return (tree: HastNode) => visit(tree);
}

/**
 * A table in a horizontally scrollable box. When columns are hidden off either edge, a shadow on that edge says so,
 * so a cut-off last column is never mistaken for the end of the table.
 */
function ScrollableTable({ layout, children }: { layout?: string; children: React.ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const [edges, setEdges] = useState({ left: false, right: false });
  const update = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const left = el.scrollLeft > 1;
    const right = el.scrollLeft + el.clientWidth < el.scrollWidth - 1;
    setEdges((prev) => (prev.left === left && prev.right === right ? prev : { left, right }));
  }, []);
  useEffect(() => {
    update();
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(update);
    observer.observe(el);
    if (el.firstElementChild) observer.observe(el.firstElementChild);
    return () => observer.disconnect();
  }, [update]);
  const variant = layout === "stacked" ? " is-stacked" : layout === "dense" ? " is-dense" : "";
  return (
    <div
      className="table-scroll"
      data-scroll-left={edges.left ? "" : undefined}
      data-scroll-right={edges.right ? "" : undefined}
    >
      <div
        ref={ref}
        className={`table-container${variant}`}
        onScroll={update}
        tabIndex={edges.left || edges.right ? 0 : undefined}
        role={edges.left || edges.right ? "region" : undefined}
        aria-label={edges.left || edges.right ? "Scrollable table" : undefined}
      >
        <table>{children}</table>
      </div>
    </div>
  );
}

/**
 * Preprocesses markdown text to ensure clean rendering:
 * 1. Normalizes raw HTML line breaks (<br>, <br/>) to valid XHTML (<br />)
 * 2. Ensures prominent unheadered section titles (e.g. "Translational & Therapeutic Implications") get proper H3 headers
 * 3. Adds spacing around tables to prevent markdown parser boundary errors
 */
function preprocessMarkdown(raw: string): string {
  if (!raw) return "";

  let processed = raw;

  // 0. Normalize legacy agent and project names to official Phronesis designations
  processed = processed
    .replace(/\bTalk2KnowledgeGraphs?\b/gi, "Synapta")
    .replace(/\bTalk2BioModels?\b/gi, "Vivarix")
    .replace(/\bTalk2Cells?\b/gi, "Lumicell")
    .replace(/\bTalk2Scholars?\b/gi, "Lithoscribe")
    .replace(/\bTalk2AI[-_ ]?Agents[-_ ]?4[-_ ]?Pharma\b/gi, "Phronesis")
    .replace(/\bAI[-_ ]?Agents[-_ ]?4[-_ ]?Pharma\b/gi, "Phronesis");



  // 1. Strip unselected placeholder sections like "No domain-specific findings were collected..."
  processed = processed.replace(
    /(?:^|\n)#{1,4}\s*[^#\n]*(?:Synapta|Vivarix|Lumicell|Lithoscribe)[^\n]*\n+No domain-specific findings were collected[^\n]*\n*/gi,
    "\n"
  );

  // 2. Strip any robotic naming convention boilerplate footers
  processed = processed.replace(
    /(?:^|\n)\*?_?All agent references follow the official naming convention[^\n]*\*?_?/gi,
    ""
  );

  // 2b. Internal hypothesis-tier labels ("`[PREDICTION: PROPOSED HYPOTHESIS - MODERATE EVIDENCE]`") read as plain words
  processed = processed.replace(
    /`?\[PREDICTION:\s*PROPOSED HYPOTHESIS\s*-\s*([^\]]+)\]`?/gi,
    (_m, tier: string) => `Hypothesis (${tier.trim().toLowerCase()})`
  );

  // 3. Normalize line breaks; remarkHtmlBreaks turns exactly this tag into a hard break (other HTML is dropped)
  processed = processed.replace(/<br\s*\/?>/gi, "<br />");

  // 4. Ensure clean line separation after table rows
  processed = processed.replace(/(\|[^\n]+\|)\n(?=[A-Za-z#])/g, "$1\n\n");

  return processed;
}

const REMARK_PLUGINS: NonNullable<MarkdownOptions["remarkPlugins"]> = [remarkGfm, remarkHtmlBreaks];

function useMarkdownComponents(citations?: CitationMap): Components {
  return useMemo<Components>(
    () => ({
      code({ className, children, ...props }) {
        const match = /language-(\w+)/.exec(className || "");
        const isInline = !match && !String(children).includes("\n");

        if (isInline) {
          return (
            <code className="inline-code" {...props}>
              {children}
            </code>
          );
        }

        const codeText = String(children).replace(/\n$/, "");
        const language = match ? match[1] : "code";

        return <CodeBlock language={language} code={codeText} />;
      },
      table({ children, ...props }) {
        const layout = (props as Record<string, unknown>)["data-layout"];
        return <ScrollableTable layout={typeof layout === "string" ? layout : undefined}>{children}</ScrollableTable>;
      },
      a({ href, children, ...props }) {
        const citeKey = (props as Record<string, unknown>)["data-cite-key"];
        if (typeof citeKey === "string" && citations) {
          return (
            <CitationRef
              citeKey={citeKey}
              occurrence={Number((props as Record<string, unknown>)["data-cite-occurrence"]) || 0}
              map={citations}
              href={href}
            >
              {children}
            </CitationRef>
          );
        }
        if (!href) return <>{children}</>;
        return (
          <a
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            className="external-link"
          >
            {children}
            <ExternalLink style={{ width: 11, height: 11, display: "inline", marginLeft: 3 }} />
          </a>
        );
      },
    }),
    [citations]
  );
}

const STREAMING_REHYPE_PLUGINS: NonNullable<MarkdownOptions["rehypePlugins"]> = [
  rehypeStripUnsafe,
  rehypeIdentifierLinkGuard,
  rehypeReadableTables,
];

const StreamingBlock = memo(function StreamingBlock({ text }: { text: string }) {
  const components = useMarkdownComponents();
  return (
    <ReactMarkdown
      skipHtml
      remarkPlugins={REMARK_PLUGINS}
      rehypePlugins={STREAMING_REHYPE_PLUGINS}
      urlTransform={safeUrlTransform}
      components={components}
    >
      {text}
    </ReactMarkdown>
  );
});

/**
 * Streaming view: finished blocks are memoized (each new token re-parses only the last block), and the
 * still-growing last block is stabilized so half-typed tables, markers and emphasis don't make the layout jump.
 */
function StreamingMarkdown({ content }: { content: string }) {
  const blocks = useMemo(() => {
    const parts = splitStreamingBlocks(content);
    if (parts.length) parts[parts.length - 1] = stabilizeStreamingTail(parts[parts.length - 1]);
    return parts;
  }, [content]);
  return (
    <div className="markdown-content">
      {blocks.map((block, i) => (block ? <StreamingBlock key={i} text={block} /> : null))}
    </div>
  );
}

function MarkdownRendererImpl({ content, citations, streaming = false }: MarkdownRendererProps) {
  const cleanedContent = useMemo(() => {
    let text = preprocessMarkdown(content);
    if (citations?.entries) {
      const urlEntries = Object.values(citations.entries).filter((e) => e && e.type === "url" && e.url);
      if (urlEntries.length > 0) {
        const primary = urlEntries[0];
        const title = primary.title || "Web Source";
        text = text.replace(/\[(?:web\s*search|web)\]/gi, `[${title}](${primary.url})`);
      }
    }
    return text;
  }, [content, citations]);
  // Raw HTML is never rendered (skipHtml + rehypeStripUnsafe); trusted transforms run after the strip.
  const rehypePlugins = useMemo<NonNullable<MarkdownOptions["rehypePlugins"]>>(
    () => [
      rehypeStripUnsafe,
      // Links whose text names an identifier but point elsewhere show their real destination (UI-018).
      [rehypeIdentifierLinkGuard, { map: citations }] as [typeof rehypeIdentifierLinkGuard, { map?: CitationMap }],
      ...(citations && (Object.keys(citations.entries || {}).length || citations.unverified?.length)
        ? [[rehypeCitations, { map: citations }] as [typeof rehypeCitations, { map: CitationMap }]]
        : []),
      rehypeReadableTables,
    ],
    [citations]
  );
  const components = useMarkdownComponents(citations);
  const rendered = useMemo(
    () =>
      streaming ? null : (
        <div className="markdown-content">
          <ReactMarkdown
            skipHtml
            remarkPlugins={REMARK_PLUGINS}
            rehypePlugins={rehypePlugins}
            urlTransform={safeUrlTransform}
            components={components}
          >
            {cleanedContent}
          </ReactMarkdown>
        </div>
      ),
    [streaming, cleanedContent, rehypePlugins, components]
  );

  if (streaming) return <StreamingMarkdown content={cleanedContent} />;
  return rendered;
}

/** Memoized: a parent re-render with the same text (and citations) never re-parses the Markdown. */
export const MarkdownRenderer = memo(MarkdownRendererImpl);

interface CodeBlockProps {
  language: string;
  code: string;
}

function CodeBlock({ language, code }: CodeBlockProps) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error("Failed to copy code:", err);
    }
  };

  return (
    <div className="code-block-wrapper">
      <div className="code-block-header">
        <div className="code-block-lang">
          <Terminal style={{ width: 12, height: 12, color: "var(--domain-t2kg)" }} />
          <span>{language}</span>
        </div>
        <button
          onClick={handleCopy}
          className="code-copy-btn"
          title="Copy code"
        >
          {copied ? (
            <>
              <Check style={{ width: 12, height: 12, color: "var(--nvidia-green)" }} />
              <span style={{ color: "var(--nvidia-green)" }}>Copied!</span>
            </>
          ) : (
            <>
              <Copy style={{ width: 12, height: 12 }} />
              <span>Copy</span>
            </>
          )}
        </button>
      </div>
      <pre>
        <code>{code}</code>
      </pre>
    </div>
  );
}
