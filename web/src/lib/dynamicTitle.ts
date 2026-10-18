/**
 * Intelligent Dynamic Title Generator for Clinical & Biomedical Conversations.
 * Extracts clean, high-signal, professional topic titles from raw user prompts.
 */

// Well-known biomedical acronyms, genes, targets and technologies that must preserve casing
const PRESERVED_ACRONYMS = new Set([
  "DNA", "RNA", "mRNA", "tRNA", "siRNA", "shRNA", "snRNA", "lncRNA",
  "KRAS", "EGFR", "TP53", "BRCA1", "BRCA2", "BRAF", "MYC", "HER2", "HER3",
  "ALK", "ROS1", "RET", "MET", "NTRK", "NTRK1", "NTRK2", "NTRK3",
  "PIK3CA", "PTEN", "AKT1", "CDK4", "CDK6", "RB1", "APC", "VHL",
  "PD-1", "PD-L1", "CTLA-4", "LAG-3", "TIM-3", "TIGIT",
  "NSCLC", "SCLC", "CRC", "PDAC", "HCC", "RCC", "TNBC", "GBM", "AML", "CML", "ALL", "CLL",
  "ODE", "SBML", "SED-ML", "PDB", "GEO", "MCP", "API", "DAG",
  "ChEMBL", "PrimeKG", "CIViC", "ClinVar", "gnomAD", "COSMIC", "TCGA",
  "UniProt", "OpenTargets", "KEGG", "PubMed", "Ensembl",
  "G12D", "G12C", "G12V", "G12A", "G12S", "G12R", "Q61H", "Q61K", "Q61L", "T790M", "C797S", "V600E"
]);

// Minor words that stay lowercase in titles (unless first word)
const MINOR_WORDS = new Set([
  "a", "an", "the", "and", "but", "or", "for", "nor", "on", "at", "to", "from",
  "by", "with", "in", "of", "vs", "versus"
]);

// Patterns of conversational preamble to strip
const PREAMBLE_PATTERNS = [
  /^(?:hello|hi|hey|good\s+(?:morning|afternoon|evening))\b[,\s.:!]*/i,
  /^(?:can|could|would|will)\s+you\s+(?:please\s+)?(?:help\s+me\s+)?(?:to\s+)?(?:explain|tell\s+me\s+(?:about)?|summarize|analyze|find|give\s+me|show\s+me|list|search|describe|discuss|compare|evaluate|check|provide|run|simulate|look\s+up)\b[,\s:]*/i,
  /^(?:please\s+)?(?:explain|tell\s+me\s+(?:about)?|summarize|analyze|find|give\s+me|show\s+me|list|search|describe|discuss|compare|evaluate|check|provide|run|simulate|look\s+up)\b[,\s:]*/i,
  /^(?:what|how|why|when|where|who)\s+(?:is|are|do|does|did|can|could|would|should|to|about|was|were)\s+/i,
  /^(?:i\s+(?:want|need|would\s+like)\s+to\s+(?:know|understand|learn|find|see|analyze|investigate|explore))\s+(?:about\s+)?/i,
  /^(?:i'm\s+looking\s+for|i\s+am\s+looking\s+for|tell\s+me\s+about|give\s+me\s+a\s+summary\s+of|can\s+we\s+look\s+at|about)\s+/i,
];

/**
 * Format a single word for title casing, respecting biomedical acronyms.
 */
function formatTitleWord(word: string, isFirst: boolean): string {
  // Check exact acronym match or uppercase match
  const upper = word.toUpperCase();
  for (const acr of PRESERVED_ACRONYMS) {
    if (upper === acr.toUpperCase()) {
      return acr;
    }
  }

  // Preserve hyphenated names (e.g. EGFR-mutant, PD-L1)
  if (word.includes("-")) {
    return word
      .split("-")
      .map((part, i) => formatTitleWord(part, isFirst && i === 0))
      .join("-");
  }

  const lower = word.toLowerCase();
  if (!isFirst && MINOR_WORDS.has(lower)) {
    return lower;
  }

  // If word is already mixed case like ChEMBL or PrimeKG, keep it
  if (/[a-z]/.test(word) && /[A-Z]/.test(word) && word.length > 2) {
    return word;
  }

  // Otherwise standard capitalize
  return lower.charAt(0).toUpperCase() + lower.slice(1);
}

/**
 * Generates an elegant, high-signal, professional title from a user inquiry.
 * Trims conversational filler, formats into Title Case, and keeps clean lengths.
 */
export function generateDynamicTitle(prompt: string, maxChars = 52): string {
  if (!prompt || typeof prompt !== "string") return "Clinical Investigation";

  const clean = prompt
    .replace(/```[\s\S]*?```/g, " ") // remove code blocks
    .replace(/https?:\/\/\S+/g, " ")   // remove URLs
    .replace(/\s+/g, " ")
    .trim();

  if (!clean) return "Clinical Investigation";

  // Iteratively strip conversational preambles
  let stripped = clean;
  let changed = true;
  let iterations = 0;
  while (changed && iterations < 4) {
    changed = false;
    iterations++;
    for (const pat of PREAMBLE_PATTERNS) {
      if (pat.test(stripped)) {
        stripped = stripped.replace(pat, "").trim();
        changed = true;
      }
    }
    // Also strip leading articles like "the ", "a ", "an " if remaining text is substantial
    if (/^(?:the|a|an)\s+/i.test(stripped) && stripped.length > 8) {
      stripped = stripped.replace(/^(?:the|a|an)\s+/i, "").trim();
      changed = true;
    }
  }

  // If preamble stripping left very little, fall back to original clean text
  const target = stripped.length >= 3 ? stripped : clean;

  // Clean trailing punctuation: question marks, colons, dashes, ellipses, periods
  const normalized = target
    .replace(/^["'`\s\-_=]+/, "")
    .replace(/[?!:;.,'"'`\s\-_=]+$/, "")
    .trim();

  if (!normalized) return "Clinical Investigation";

  // Split into words and format
  const words = normalized.split(/\s+/).filter(Boolean);
  if (words.length === 0) return "Clinical Investigation";

  // Format words with biomedical awareness
  const formattedWords = words.map((w, idx) => formatTitleWord(w, idx === 0));

  // Build title up to maxChars
  let result = "";
  for (const w of formattedWords) {
    const candidate = result ? `${result} ${w}` : w;
    if (candidate.length > maxChars) {
      // If we already have at least 2 words, stop at word boundary
      if (result.split(" ").length >= 2) break;
      // Otherwise allow current word or cut
      result = candidate.slice(0, maxChars).replace(/[,.:;_\-\s]+$/, "");
      break;
    }
    result = candidate;
  }

  // Clean trailing punctuation left by a word-boundary cut ("…Lung Cancer,") and trailing minor words
  const trailingCleaned = result
    .replace(/[,.:;\-\s]+$/, "")
    .replace(/\s+(?:in|of|and|for|on|with|to|at|by|a|the|vs)$/i, "")
    .trim();
  const finalTitle = trailingCleaned || result;

  return finalTitle || "Clinical Investigation";
}
