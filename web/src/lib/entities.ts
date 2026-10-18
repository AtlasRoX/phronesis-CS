// Display-text helpers shared by several components (kept out of component modules so hot reload and
// "use client" boundaries never leave an importer holding a stale reference).

const ENTITY_MAP: Record<string, string> = {
  amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " ", le: "≤", ge: "≥", plusmn: "±", minus: "−", ndash: "–", mdash: "—",
};

/** Text as people should read it: HTML entities from abstracts decoded ("&lt;0.001" -> "<0.001"). */
export function decodeEntities(text: string | null | undefined): string {
  return String(text ?? "").replace(/&(#\d{1,7}|#x[0-9a-f]{1,6}|[a-z]{2,8});/gi, (m, code: string) => {
    if (code[0] === "#") {
      const n = code[1] === "x" || code[1] === "X" ? parseInt(code.slice(2), 16) : parseInt(code.slice(1), 10);
      return Number.isFinite(n) && n > 0 && n < 0x110000 ? String.fromCodePoint(n) : m;
    }
    return ENTITY_MAP[code.toLowerCase()] ?? m;
  });
}

// Verifier defect categories that older runs wrote into step text as "[CATEGORY] claim".
const DEFECT_LABELS: Record<string, string> = {
  CLAIM_CONTRADICTED: "Contradicted by a source:",
  CLINICAL_CLAIM: "Clinical claim not found in the evidence:",
  POPULATION_MISMATCH: "Population mismatch:",
  UNSUPPORTED_CLAIM: "Not found in the evidence:",
  UNVERIFIED_IDENTIFIER: "Identifier not in the evidence:",
  NUMERIC_MISMATCH: "Number does not match the evidence:",
  NUMERIC_RELATION_MISMATCH: "Number belongs to a different group, arm or endpoint:",
  REGIMEN_MISMATCH: "Regimen mislabelled:",
  CITATION_MISMATCH: "Cited source does not support this:",
};

/** Step text for display: internal "[CATEGORY]" prefixes become plain labels and list markers are dropped. */
export function cleanStepText(text: string | null | undefined): string {
  return decodeEntities(text)
    .split("\n")
    .map((line) =>
      line.replace(/^\s*\[([A-Z][A-Z_]{3,})\]\s*(?:[-*•]\s+)?/, (_m, cat: string) => {
        const label = DEFECT_LABELS[cat] ?? `${cat.charAt(0)}${cat.slice(1).toLowerCase().replace(/_/g, " ")}:`;
        return `${label} `;
      }),
    )
    .join("\n");
}
