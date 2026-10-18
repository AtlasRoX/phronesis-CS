// Column choice and cell sizing for the generic agentic tables (no React or CSS imports, so node tests load it).

/** Placeholder values a source returns for "no value"; a column of only these says nothing. */
const EMPTY_VALUES = new Set(["", "n/a", "na", "none", "null", "undefined", "-", "—", "–", "unknown", "nan"]);

export function isEmptyCell(v: unknown): boolean {
  if (v == null) return true;
  if (typeof v === "number") return !Number.isFinite(v);
  if (typeof v === "string") return EMPTY_VALUES.has(v.trim().toLowerCase());
  return false;
}

function isScalar(v: unknown): boolean {
  return v == null || ["string", "number", "boolean"].includes(typeof v);
}

/** A list of scalars (evidence types, PMIDs, cell lines) is shown as joined text; nested objects are not tabulated. */
export function isScalarList(v: unknown): v is unknown[] {
  return Array.isArray(v) && v.every((x) => x == null || ["string", "number", "boolean"].includes(typeof x));
}

/** Cell text for a scalar list ("a, b, c"); "" for an empty list. */
export function listText(v: unknown[]): string {
  return v.filter((x) => x != null && x !== "").map(String).join(", ");
}

function hasValue(v: unknown): boolean {
  return isScalarList(v) ? listText(v) !== "" : !isEmptyCell(v);
}

/**
 * Every displayable column of `rows` (scalars and scalar lists) in first-seen order, without columns whose every
 * value is empty or "n/a". `priority` columns (e.g. a p-value or confidence) are moved to the front.
 */
export function displayableColumns(rows: Record<string, unknown>[], priority: string[] = []): string[] {
  const cols: string[] = [];
  for (const r of rows) {
    for (const [k, v] of Object.entries(r)) {
      if (!cols.includes(k) && (isScalar(v) || isScalarList(v))) cols.push(k);
    }
  }
  const shown = cols.filter((c) => rows.some((r) => hasValue(r[c])));
  const first = priority.filter((c) => shown.includes(c));
  return [...first, ...shown.filter((c) => !first.includes(c))];
}

/** Columns to show (at most `maxCols`) and the ones left out, so the table can say "N more columns" (UI-022). */
export function tableLayout(
  rows: Record<string, unknown>[],
  maxCols = 6,
  priority: string[] = []
): { columns: string[]; hidden: string[] } {
  const all = displayableColumns(rows, priority);
  return { columns: all.slice(0, maxCols), hidden: all.slice(maxCols) };
}

/** Scalar columns of `rows` in first-seen order, without columns whose every value is empty or "n/a", at most `maxCols`. */
export function visibleColumns(rows: Record<string, unknown>[], maxCols = 6): string[] {
  const cols: string[] = [];
  for (const r of rows) {
    for (const [k, v] of Object.entries(r)) {
      if (!cols.includes(k) && isScalar(v)) cols.push(k);
    }
  }
  return cols.filter((c) => rows.some((r) => !isEmptyCell(r[c]))).slice(0, maxCols);
}

export type CellKind = "num" | "short" | "text" | "long";

/** How a column's cells are laid out: numbers right-aligned, short labels on one line, long text wrapping. Only
 * long text gets a minimum width, so one long column wraps inside the card instead of forcing a horizontal scroll. */
export function columnKind(rows: Record<string, unknown>[], col: string): CellKind {
  const values = rows.map((r) => (isScalarList(r[col]) ? listText(r[col] as unknown[]) : r[col])).filter((v) => !isEmptyCell(v));
  if (values.length && values.every((v) => typeof v === "number")) return "num";
  const longest = Math.max(0, ...values.map((v) => String(v).length));
  if (longest <= 24) return "short";
  return longest <= 60 ? "text" : "long";
}
