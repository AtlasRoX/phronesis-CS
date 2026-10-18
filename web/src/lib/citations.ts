// Citation helpers for the answer renderer (owned by CITATIONS, feature 4).
// Keep this module framework-free and erasable-TypeScript-only so `node --test` can import it.
// The identifier grammar mirrors phronesis/agents/citations.py so occurrence counts line up.

import type { CitationEntry, CitationMap, ClaimSupport } from "../types/citations";

type HastNode = {
  type: string;
  tagName?: string;
  value?: string;
  properties?: Record<string, unknown>;
  children?: HastNode[];
};

/**
 * Cited identifiers. Groups: 1 PMID, 2 DOI, 3 NCT, 4 CHEMBL, 5 BIOMD, 6 ENSG, 7 KEGG, 8 VCV, 9 rsID,
 * 10 CIViC EID/AID, 11 GEO, 12 DrugBank. Global: reset lastIndex (or use matchAll) before reuse.
 */
export const CITE_PATTERN =
  /\b[Pp][Mm][Ii][Dd]:?\s*(\d{1,9})\b|(\b10\.\d{4,9}\/[-._;()\/:A-Za-z0-9]+\b)|\b([Nn][Cc][Tt]\d{8})\b|\b([Cc][Hh][Ee][Mm][Bb][Ll]\d{1,9})\b|\b(BIOMD\d{10})\b|\b(ENSG\d{11})\b|\b(hsa\d{5})\b|\b(VCV\d{9})(?:\.\d+)?\b|\b(rs\d{3,12})\b|\b((?:EID|AID)\d{1,6})\b|\b(GS[EM]\d{1,9})\b|\b(DB\d{5})\b/g;

const GROUP_PREFIX = ["", "PMID", "DOI", "NCT", "CHEMBL", "BIOMD", "ENSG", "KEGG", "VCV", "RS", "CIVIC", "GEO", "DRUGBANK"];

const ID_PATTERNS: Record<string, RegExp> = {
  PMID: /^\d{1,9}$/,
  DOI: /^10\.\d{4,9}\/[-._;()\/:a-z0-9]+$/,
  NCT: /^NCT\d{8}$/,
  CHEMBL: /^CHEMBL\d{1,9}$/,
  BIOMD: /^BIOMD\d{10}$/,
  ENSG: /^ENSG\d{11}$/,
  KEGG: /^hsa\d{5}$/,
  VCV: /^VCV\d{9}$/,
  RS: /^rs\d{3,12}$/,
  EID: /^EID\d{1,6}$/,
  AID: /^AID\d{1,6}$/,
  GEO: /^GS[EM]\d{1,9}$/,
  DRUGBANK: /^DB\d{5}$/,
};

const VALID_URL = /^https?:\/\/[A-Za-z0-9.-]+(?::\d{1,5})?(?:[/?#][^\s<>"'`]*)?$/i;

/** URL identity used for keys: no fragment, no trailing slash (same rule as the backend). */
export function normalizeUrl(url: string): string {
  return url.trim().split("#", 1)[0].replace(/\/+$/, "");
}

/** Canonical "PREFIX:id" key for a raw id, or null when it does not validate. */
export function makeKey(prefix: string, rawId: string): string | null {
  let value = String(rawId ?? "").trim();
  if (!value) return null;
  if (prefix === "URL") return VALID_URL.test(value) ? `URL:${normalizeUrl(value)}` : null;
  if (prefix === "DOI") {
    value = value
      .replace(/^(?:https?:\/\/(?:dx\.)?doi\.org\/|doi:\s*)/i, "")
      .replace(/[.,;]+$/, "")
      .toLowerCase();
  } else if (
    prefix === "NCT" || prefix === "CHEMBL" || prefix === "EID" || prefix === "AID" || prefix === "GEO" ||
    prefix === "DRUGBANK"
  ) {
    value = value.toUpperCase();
  } else if (prefix === "VCV") {
    value = value.split(".", 1)[0].toUpperCase();
  } else if (prefix === "RS") {
    value = value.toLowerCase();
  }
  const pattern = ID_PATTERNS[prefix];
  return pattern && pattern.test(value) ? `${prefix}:${value}` : null;
}

function keyFromMatch(m: RegExpExecArray | RegExpMatchArray): string | null {
  for (let g = 1; g < GROUP_PREFIX.length; g++) {
    const value = m[g];
    if (value === undefined) continue;
    if (g === 2) return makeKey("DOI", value.replace(/[.,;)]+$/, ""));
    if (g === 10) return makeKey(value.slice(0, 3), value);
    return makeKey(GROUP_PREFIX[g], value);
  }
  return null;
}

/** Key for the first identifier in `text` ("PMID: 123" -> "PMID:123", DOIs lowercased, trailing .,;) dropped). */
export function toCiteKey(text: string): string | null {
  const re = new RegExp(CITE_PATTERN.source);
  const m = re.exec(String(text ?? ""));
  return m ? keyFromMatch(m) : null;
}

const HREF_PATTERNS: Array<[RegExp, string]> = [
  [/^https?:\/\/pubmed\.ncbi\.nlm\.nih\.gov\/(\d{1,9})\/?(?:[?#].*)?$/i, "PMID"],
  [/^https?:\/\/(?:dx\.)?doi\.org\/(10\.[^?#]+)$/i, "DOI"],
  [/^https?:\/\/(?:www\.)?clinicaltrials\.gov\/(?:study|ct2\/show)\/(NCT\d{8})\/?(?:[?#].*)?$/i, "NCT"],
  [/^https?:\/\/www\.ebi\.ac\.uk\/chembl\/(?:explore\/)?compound(?:_report_card)?\/(CHEMBL\d{1,9})\/?(?:[?#].*)?$/i, "CHEMBL"],
  [/^https?:\/\/(?:www\.)?(?:biomodels\.org|ebi\.ac\.uk\/biomodels)\/(BIOMD\d{10})\/?(?:[?#].*)?$/i, "BIOMD"],
  [/^https?:\/\/platform\.opentargets\.org\/target\/(ENSG\d{11})\/?(?:[?#].*)?$/i, "ENSG"],
];

/** Key for an answer link: a database link-out maps to its identifier, any other http(s) link to "URL:". */
export function hrefToKey(href: unknown): string | null {
  const value = typeof href === "string" ? href.trim() : "";
  if (!value) return null;
  for (const [pattern, prefix] of HREF_PATTERNS) {
    const m = pattern.exec(value);
    if (m) {
      let id = m[1];
      if (prefix === "DOI") {
        try {
          id = decodeURIComponent(id);
        } catch {
          return null;
        }
      }
      return makeKey(prefix, id);
    }
  }
  return makeKey("URL", value);
}

export function resolveKey(map: CitationMap, key: string): string {
  return (map.aliases && map.aliases[key]) || key;
}

export function entryFor(map: CitationMap, key: string): CitationEntry | undefined {
  return map.entries ? map.entries[resolveKey(map, key)] : undefined;
}

/**
 * The claim support for this occurrence. When client and server occurrence counts drifted there is no exact match;
 * the key's best other support is then returned flagged `fallback: true`, so the popover says it belongs to another
 * mention of the source and never presents it as this sentence's verifier match (UI-017).
 */
export function supportFor(map: CitationMap, key: string, occurrence: number): ClaimSupport | null {
  const resolved = resolveKey(map, key);
  const all = (map.supports || []).filter((s) => s.key === resolved);
  const exact = all.find((s) => s.occurrence === occurrence);
  if (exact) return exact;
  const other = all.find((s) => s.grounded_by_verifier) || all.find((s) => s.highlight) || all[0] || null;
  return other ? { ...other, fallback: true } : null;
}

/** http(s) links only; anything else is not rendered as a link-out. */
export function safeHref(url: unknown): string | undefined {
  return typeof url === "string" && /^https?:\/\//i.test(url.trim()) ? url.trim() : undefined;
}

function hastText(node: HastNode): string {
  if (node.type === "text") return node.value || "";
  return (node.children || []).map(hastText).join("");
}

/** "PMID:123" -> "PMID 123"; "URL:https://evil.example/x" -> "evil.example". */
function describeKey(key: string): string {
  if (key.startsWith("URL:")) {
    const m = /^URL:https?:\/\/([^/?#:]+)/i.exec(key);
    return m ? m[1].toLowerCase() : "another site";
  }
  const i = key.indexOf(":");
  return i > 0 ? `${key.slice(0, i)} ${key.slice(i + 1)}` : key;
}

/**
 * rehype plugin (run after rehypeStripUnsafe, before rehypeCitations) for links whose visible text names an
 * identifier (UI-018). Link text is never scanned for citations, so `[PMID 12345](https://evil.example/x)` would
 * read as the PubMed record while opening any page. When the identifier in the text is not what the href points to,
 * a visible "(links to …)" suffix names the real destination. `map` (optional) resolves aliases (DOI -> PMID).
 */
export function rehypeIdentifierLinkGuard(options?: { map?: CitationMap }) {
  const map = options && options.map;
  const canon = (key: string) => (map ? resolveKey(map, key) : key);
  const visit = (node: HastNode) => {
    for (const child of node.children || []) {
      if (child.type !== "element") continue;
      if (child.tagName === "a") {
        const href = child.properties ? child.properties.href : undefined;
        const textKey = toCiteKey(hastText(child));
        if (!textKey || typeof href !== "string" || !href) continue;
        const hrefKey = hrefToKey(href);
        if (hrefKey && canon(hrefKey) === canon(textKey)) continue;
        // Relative / unrecognised hrefs have no key: name the destination as given.
        const where = hrefKey ? describeKey(hrefKey) : href.slice(0, 60);
        child.properties = { ...child.properties, dataLinkMismatch: "", title: `Opens ${href}` };
        child.children = [
          ...(child.children || []),
          { type: "element", tagName: "span", properties: { className: ["link-destination"] }, children: [{ type: "text", value: ` (links to ${where})` }] },
        ];
        continue;
      }
      visit(child);
    }
  };
  return (tree: unknown) => {
    if (tree) visit(tree as HastNode);
  };
}

const SKIP_TAGS = new Set(["code", "pre", "a", "kbd", "samp", "script", "style"]);
const UNVERIFIED_STYLE =
  "text-decoration-line:underline;text-decoration-style:dotted;text-underline-offset:3px;cursor:help";

/**
 * rehype plugin (run after rehypeStripUnsafe) that wraps cited identifiers in anchors carrying the hast properties
 * dataCiteKey / dataCiteOccurrence (rendered as data-cite-key / data-cite-occurrence). Only keys present in
 * map.entries are linked; ids the verifier rejected get a dotted underline and no link. Text inside
 * code/pre/a/kbd/samp/script/style is never touched. Existing links whose href is a known entry are
 * annotated in place, and occurrences are counted per key in document order.
 */
export function rehypeCitations(options: { map: CitationMap }) {
  const map = options && options.map;
  return (tree: unknown) => {
    if (!map || !map.entries || !tree) return;
    const counts = new Map<string, number>();
    const unverified = new Set(map.unverified || []);
    const next = (key: string) => {
      const n = counts.get(key) || 0;
      counts.set(key, n + 1);
      return n;
    };

    const splitText = (value: string): HastNode[] => {
      const out: HastNode[] = [];
      let last = 0;
      for (const m of value.matchAll(CITE_PATTERN)) {
        const raw = keyFromMatch(m);
        if (!raw) continue;
        const key = resolveKey(map, raw);
        const occurrence = next(key);
        const start = m.index ?? 0;
        const entry = map.entries[key];
        let node: HastNode | null = null;
        if (entry && safeHref(entry.url)) {
          node = {
            type: "element",
            tagName: "a",
            properties: {
              href: entry.url,
              className: ["cite-ref"],
              dataCiteKey: key,
              dataCiteOccurrence: String(occurrence),
            },
            children: [{ type: "text", value: m[0] }],
          };
        } else if (unverified.has(key)) {
          node = {
            type: "element",
            tagName: "span",
            // Covers ids never observed and retracted ids the Verifier vetoed; the notice above the answer says which.
            // The note is also real (visually hidden) text, so screen-reader and touch users get it, not only a hover title (UI-019).
            properties: { className: ["cite-unverified"], title: "Not verified against the retrieved evidence", style: UNVERIFIED_STYLE },
            children: [
              { type: "text", value: m[0] },
              { type: "element", tagName: "span", properties: { className: ["sr-only"] }, children: [{ type: "text", value: " (not verified against the retrieved evidence)" }] },
            ],
          };
        }
        if (!node) continue;
        if (start > last) out.push({ type: "text", value: value.slice(last, start) });
        out.push(node);
        last = start + m[0].length;
      }
      if (!out.length) return [{ type: "text", value }];
      if (last < value.length) out.push({ type: "text", value: value.slice(last) });
      return out;
    };

    const annotateLink = (el: HastNode) => {
      const props = el.properties || {};
      const raw = hrefToKey(props.href);
      if (!raw) return;
      const key = resolveKey(map, raw);
      const occurrence = next(key);
      if (!map.entries[key]) return;
      const existing = props.className;
      const classes = Array.isArray(existing) ? existing.map(String) : typeof existing === "string" ? existing.split(/\s+/) : [];
      el.properties = {
        ...props,
        className: [...classes.filter(Boolean), "cite-ref"],
        dataCiteKey: key,
        dataCiteOccurrence: String(occurrence),
      };
    };

    const walk = (node: HastNode) => {
      const children = node.children;
      if (!children || !children.length) return;
      const out: HastNode[] = [];
      for (const child of children) {
        if (child.type === "text" && typeof child.value === "string") {
          out.push(...splitText(child.value));
          continue;
        }
        if (child.type === "element") {
          if (child.tagName === "a") annotateLink(child);
          else if (!SKIP_TAGS.has(child.tagName || "")) walk(child);
        } else if (child.type === "root") {
          walk(child);
        }
        out.push(child);
      }
      node.children = out;
    };

    walk(tree as HastNode);
  };
}
