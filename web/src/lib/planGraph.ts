// Pure layout helpers for the agent plan DAG (LLMCompiler task graph), kept free of React for node tests.

export interface DagNode {
  id: string;
  depends_on?: unknown;
}

/** Layer index per node = longest dependency chain from a root. Unknown deps are ignored; cycles are capped. */
export function layerNodes(nodes: DagNode[]): Map<string, number> {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const depth = new Map<string, number>();
  const visiting = new Set<string>();
  const depsOf = (id: string): string[] => {
    const raw = byId.get(id)?.depends_on;
    return Array.isArray(raw) ? raw.filter((d): d is string => typeof d === "string" && d !== id && byId.has(d)) : [];
  };
  const visit = (id: string): number => {
    const known = depth.get(id);
    if (known !== undefined) return known;
    if (visiting.has(id)) return 0; // cycle guard: the validator rejects cycles, but never loop on bad data
    visiting.add(id);
    const deps = depsOf(id);
    const d = deps.length ? Math.max(...deps.map(visit)) + 1 : 0;
    visiting.delete(id);
    depth.set(id, d);
    return d;
  };
  nodes.forEach((n) => visit(n.id));
  return depth;
}
