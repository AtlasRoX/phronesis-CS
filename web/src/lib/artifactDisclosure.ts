// Which result cards open by default under an answer. Deterministic: the question text, the task outcome and each
// artifact's own state decide; no model call and no network. Framework-free so `node --test` can import it.
//
// The rule of thumb: evidence the question asked for, warnings (low support, contradictions, failures) and unfinished
// work open; routine diagnostics (agent dialogue, project memory, artifacts nobody asked for) stay compact. A reader's
// own open/close choice always wins over this policy (see Card in components/agentic/ui.tsx).

type Data = Record<string, any> | null | undefined;

export interface DisclosureInput {
  /** The user's question this answer responds to. */
  question?: string | null;
  /** The answer's task outcome (task_status, incomplete_reasons, unresolved). */
  taskStatus?: { task_status?: string; incomplete_reasons?: unknown; unresolved?: unknown } | null;
  artifacts?: Record<string, any> | null;
}

/** Domain artifacts open only when the question asks for that kind of evidence or analysis. */
const TOPICS: Record<string, RegExp> = {
  drug_intel:
    /\b(?:fda|approv\w*|labels?|labell?ing|indicat\w*|regulat\w*|drugs?@fda|safety|adverse|faers|boxed\s+warnings?|contraindicat\w*|pharmacovigil\w*|toxicit\w*)\b/i,
  clinical_trials: /\b(?:trials?|nct\d{8}|recruit\w*|enrol\w*|clinicaltrials(?:\.gov)?)\b/i,
  variant_intel: /\b(?:variants?|mutations?|mutant|alleles?|clinvar|civic|gnomad|pathogenic\w*|g12[a-z]|v600e)\b/i,
  disease_targets: /\b(?:targets?|druggab\w*|open\s*targets|target[\s-]+disease|associations?)\b/i,
  primekg: /\b(?:knowledge\s+graph|primekg|repurpos\w*|graph|network\s+medicine)\b/i,
  structure_intel: /\b(?:structures?|structural|pdb|alphafold|docking|binding\s+(?:site|pocket)|pockets?|ligands?)\b/i,
  omics_intel:
    /\b(?:omics|expression|gtex|depmap|gwas|transcriptom\w*|proteom\w*|co-?expression|string\s+network|reactome|essentiality)\b/i,
  lumicell: /\b(?:single[\s-]?cell|scrna(?:-seq)?|snrna(?:-seq)?|h5ad|cell\s+types?|clusters?|marker\s+genes?)\b/i,
  vivarix: /\b(?:simulat\w*|time[\s-]?courses?|steady[\s-]?states?|kinetic\w*|odes?|biomodels?|sbml|parameter\s+(?:scans?|sweeps?))\b/i,
  hypotheses: /\b(?:hypothes[ie]s|hypothesi[sz]\w*|testable|propose\s+(?:a|two|three|new|novel))\b/i,
};
TOPICS.vivarix_scan = TOPICS.vivarix;

/** Words naming each topic inside an exclusion clause ("No FDA sweep, simulation or omics is needed"). */
const EXCLUSION_WORDS: Record<string, RegExp> = {
  drug_intel: /\b(?:fda|regulat\w*|drug\s+safety|safety|label\w*|faers)\b/i,
  clinical_trials: /\btrials?\b/i,
  variant_intel: /\b(?:variants?|mutations?|clinvar)\b/i,
  disease_targets: /\btargets?\b/i,
  primekg: /\b(?:knowledge\s+graph|graph|primekg|repurpos\w*)\b/i,
  structure_intel: /\b(?:structures?|structural|docking|pdb|alphafold)\b/i,
  omics_intel: /\bomics\b|\b(?:expression|gtex|depmap|gwas)\b/i,
  lumicell: /\b(?:single[\s-]?cell|scrna\w*|snrna\w*)\b/i,
  vivarix: /\b(?:simulat\w*|time[\s-]?courses?|kinetic\w*|model(?:l?ing)?)\b/i,
  hypotheses: /\b(?:hypothes[ie]s|hypothesi[sz]\w*)\b/i,
};
EXCLUSION_WORDS.vivarix_scan = EXCLUSION_WORDS.vivarix;

const NEEDED = /\b(?:needed|required|necessary|wanted|requested)\b/i;

/**
 * The parts of the question that exclude work: sentences or clauses such as "No simulation or single-cell analysis
 * needed.", "without running simulations", "do not run a debate", "Simulation is not needed". A plain "no" in other
 * wording ("if no exact lookup works") is only an exclusion when it opens a sentence or the clause says "needed".
 */
export function exclusionClauses(question: string): string[] {
  const out: string[] = [];
  const sentences = question.split(/(?<=[.!?;])\s+|\n+/);
  for (const raw of sentences) {
    const s = raw.trim();
    if (!s) continue;
    if (/^no\s+/i.test(s) && !/^no\s+(?:more\s+than|later\s+than|fewer\s+than)\b/i.test(s)) {
      out.push(s);
      continue;
    }
    const noNeeded = s.match(/\bno\s+[^.;:!?]*$/i);
    if (noNeeded && NEEDED.test(noNeeded[0])) out.push(noNeeded[0]);
    const without = s.match(/\b(?:without|skip(?:ping)?|exclud(?:e|ing)|avoid(?:ing)?)\s+[^.;:!?,]*/gi);
    if (without) out.push(...without);
    const dont = s.match(/\b(?:do\s+not|don'?t|never|no\s+need\s+to)\s+(?:run|perform|do|include|use|need|start|launch|add)\s+[^.;:!?]*/gi);
    if (dont) out.push(...dont);
    const notNeeded = s.match(/[^.;:!?]*\b(?:is|are)\s+not\s+(?:needed|required|necessary|wanted)\b/gi);
    if (notNeeded) out.push(...notNeeded);
  }
  return out;
}

/** Whether the question asks for this artifact's kind of evidence and does not explicitly exclude it. */
export function questionRequests(key: string, question: string | null | undefined): boolean {
  const q = (question || "").trim();
  const topic = TOPICS[key];
  if (!q || !topic) return false;
  const clauses = exclusionClauses(q);
  const words = EXCLUSION_WORDS[key];
  if (words && clauses.some((c) => words.test(c))) return false;
  let rest = q;
  for (const c of clauses) rest = rest.split(c).join(" ");
  return topic.test(rest);
}

function failed(data: Data): boolean {
  if (!data || typeof data !== "object") return false;
  if (data.success === false) return true;
  const status = String(data.status || data.task_status || "").toLowerCase();
  if (/(?:^|_)(?:error|failed|failure)$/.test(status)) return true;
  return typeof data.error === "string" && data.error.trim().length > 0;
}

function list(value: unknown): any[] {
  return Array.isArray(value) ? value : [];
}

/** Partial answers, unanswered sub-questions, failed calls or phases, and a used-up budget. */
export function runNeedsAttention(run: Data, taskStatus?: DisclosureInput["taskStatus"]): boolean {
  if (taskStatus) {
    if (taskStatus.task_status && taskStatus.task_status !== "completed") return true;
    if (list(taskStatus.incomplete_reasons).length || list(taskStatus.unresolved).length) return true;
  }
  if (!run || typeof run !== "object") return false;
  if (run.budget?.exhausted) return true;
  if (list(run.sub_questions).some((q) => q && q.status && !["answered", "done"].includes(String(q.status)))) return true;
  if (list(run.trace).some((t) => t && t.success === false)) return true;
  return list(run.phases).some((p) => p && /^(?:error|budget|timeout|llm_unavailable|cancelled)$/.test(String(p.status || "")));
}

export function planNeedsAttention(plan: Data): boolean {
  if (!plan || typeof plan !== "object") return false;
  if (list(plan.rejected).length) return true;
  if (plan.planner === "none" || /fail|unusable|budget/.test(String(plan.status || ""))) return true;
  return list(plan.nodes).some((n) => n && String(n.status || "") === "error");
}

/** Claim-support scores: a low-support claim, or overall support below 0.6 (support scores, not truth probabilities). */
export function lowSupport(conf: Data): boolean {
  if (!conf || typeof conf !== "object") return false;
  if (typeof conf.answer_confidence === "number" && conf.answer_confidence < 0.6) return true;
  if (typeof conf.supported_fraction === "number" && conf.supported_fraction < 0.6) return true;
  return list(conf.claims).some((c) => c && (c.band === "low" || (typeof c.confidence === "number" && c.confidence < 0.5)));
}

/** Counter-evidence: a contradicting passage, or a heuristic-only classification that deserves a second look. */
export function conflictsNeedAttention(conflicts: Data): boolean {
  if (!conflicts || typeof conflicts !== "object") return false;
  if (list(conflicts.conflicts).some((c) => c && String(c.label || "") === "contradicts")) return true;
  return String(conflicts.classifier || "") === "heuristic" && list(conflicts.conflicts).length > 0;
}

/** Default open state for every artifact present in `artifacts`, keyed like ExecutionArtifacts. */
export function artifactDisclosure(input: DisclosureInput): Record<string, boolean> {
  const arts = input.artifacts || {};
  const q = input.question || "";
  const out: Record<string, boolean> = {};
  for (const [key, data] of Object.entries(arts)) {
    if (data === null || data === undefined) continue;
    switch (key) {
      case "plan_approval":
      case "literature":
      case "sandbox_analysis":
      case "model_calibration":
        out[key] = true;
        break;
      case "evidence_conflicts":
        out[key] = conflictsNeedAttention(data);
        break;
      case "claim_confidence":
        out[key] = lowSupport(data);
        break;
      case "agent_run":
        out[key] = runNeedsAttention(data, input.taskStatus);
        break;
      case "agent_plan":
        out[key] = planNeedsAttention(data);
        break;
      case "agent_dialogue":
      case "project_memory":
        out[key] = false;
        break;
      default:
        if (TOPICS[key]) out[key] = failed(data) || questionRequests(key, q);
    }
  }
  return out;
}
