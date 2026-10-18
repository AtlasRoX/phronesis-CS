"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { Ban, Brain, ChevronDown, FolderOpen, FolderPlus, RefreshCw, Search, Trash2, X } from "lucide-react";
import type { AuthHeaders } from "@/services/datasetService";
import { createProject, deleteFact, getProjectMemory, listProjects, patchFact } from "@/services/agentService";
import type { MemoryFact, Project } from "@/types/agentic";
import { dateText, factText, sourceText } from "./ProjectMemoryArtifact";
import { Pill, fmt, isNum, str, styles } from "./ui";

/** Research project picker (F16). The selected project's id is sent as project_id with agentic requests. */
export function ProjectSelector({
  backendUrl,
  authHeaders,
  projectId,
  onChange,
}: {
  backendUrl: string;
  authHeaders: AuthHeaders;
  projectId: string | null;
  onChange: (projectId: string | null, project?: Project) => void;
}) {
  const [open, setOpen] = useState(false);
  const [projects, setProjects] = useState<Project[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [creating, setCreating] = useState(false);
  const [memoryOpen, setMemoryOpen] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);
  const authRef = useRef(authHeaders);
  authRef.current = authHeaders;

  const load = useCallback(async () => {
    try {
      setProjects(await listProjects(backendUrl, authRef.current));
      setError(null);
    } catch (err: any) {
      setError(err?.status === 404 ? "Projects are not available on this server." : err?.message || "Projects could not be loaded.");
    } finally {
      setLoaded(true);
    }
  }, [backendUrl]);

  // Load once when a project is already selected, so the button can show its name.
  useEffect(() => {
    if (projectId && !loaded) void load();
  }, [projectId, loaded, load]);

  useEffect(() => {
    if (!open) return;
    void load();
    const onDown = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, load]);

  const current = projects.find((p) => p.id === projectId) || null;

  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim() || creating) return;
    setCreating(true);
    try {
      const p = await createProject(backendUrl, authHeaders, name.trim().slice(0, 200), description.trim().slice(0, 2000));
      setProjects((list) => [p, ...list.filter((x) => x.id !== p.id)]);
      setName("");
      setDescription("");
      onChange(p.id, p);
      setError(null);
    } catch (err: any) {
      setError(err?.message || "The project could not be created.");
    } finally {
      setCreating(false);
    }
  };

  return (
    <div ref={boxRef} style={{ position: "relative" }}>
      <button
        type="button"
        className={styles.headerBtn}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        title={projectId ? `Project: ${current?.name || projectId}` : "Select a research project (project memory)"}
      >
        <FolderOpen style={{ width: 15, height: 15, color: projectId ? "var(--domain-scp)" : undefined, flexShrink: 0 }} aria-hidden="true" />
        <span className={`hidden sm:inline ${styles.truncate}`}>{projectId ? current?.name || projectId : "Project"}</span>
        <ChevronDown style={{ width: 12, height: 12, flexShrink: 0 }} aria-hidden="true" />
      </button>

      {open && (
        <div className={styles.popover} role="dialog" aria-label="Research projects">
          <div className={styles.row}>
            <span className={`${styles.sectionTitle} ${styles.rowGrow}`} style={{ margin: 0 }}>
              Research project
            </span>
            <button type="button" className={styles.btn} onClick={() => void load()} aria-label="Refresh projects">
              <RefreshCw style={{ width: 12, height: 12 }} aria-hidden="true" />
            </button>
          </div>
          <p className={styles.note} style={{ marginTop: 0 }}>
            Agentic answers in a project remember verified facts and recall them in later questions.
          </p>
          <button
            type="button"
            className={`${styles.itemBox} ${projectId ? "" : styles.itemBoxActive}`}
            style={{ textAlign: "left", cursor: "pointer", font: "inherit" }}
            onClick={() => {
              onChange(null);
              setOpen(false);
            }}
            aria-pressed={!projectId}
          >
            No project
          </button>
          {projects.map((p) => (
            <button
              key={p.id}
              type="button"
              className={`${styles.itemBox} ${p.id === projectId ? styles.itemBoxActive : ""}`}
              style={{ textAlign: "left", cursor: "pointer", font: "inherit" }}
              onClick={() => {
                onChange(p.id, p);
                setOpen(false);
              }}
              aria-pressed={p.id === projectId}
            >
              <div className={styles.truncate} style={{ color: "var(--text-heading)", fontWeight: 600 }}>
                {p.name}
              </div>
              <div className={`${styles.meta} ${styles.truncate}`}>
                {[p.description, isNum(p.fact_count) ? `${p.fact_count} facts` : "", p.id].filter(Boolean).join(" · ")}
              </div>
            </button>
          ))}
          {projectId && (
            <button
              type="button"
              className={styles.btn}
              onClick={() => {
                setMemoryOpen(true);
                setOpen(false);
              }}
            >
              <Brain style={{ width: 12, height: 12 }} aria-hidden="true" /> View project memory
            </button>
          )}
          <form onSubmit={create} style={{ display: "flex", flexDirection: "column", gap: 6, borderTop: "1px solid var(--border-subtle)", paddingTop: 8 }}>
            <input className={styles.input} value={name} onChange={(e) => setName(e.target.value)} placeholder="New project name" aria-label="New project name" maxLength={200} />
            <input
              className={styles.input}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Description (optional)"
              aria-label="New project description"
              maxLength={2000}
            />
            <button type="submit" className={styles.btn} disabled={!name.trim() || creating} style={{ alignSelf: "flex-start" }}>
              <FolderPlus style={{ width: 12, height: 12 }} aria-hidden="true" /> {creating ? "Creating…" : "Create project"}
            </button>
          </form>
          {error && (
            <p className={styles.errorText} role="alert">
              {error}
            </p>
          )}
        </div>
      )}

      {memoryOpen && projectId && (
        <ProjectMemoryDrawer
          backendUrl={backendUrl}
          authHeaders={authHeaders}
          projectId={projectId}
          projectName={current?.name || projectId}
          onClose={() => setMemoryOpen(false)}
        />
      )}
    </div>
  );
}

/** Lists a project's facts with search, invalidate (kept, marked superseded) and delete. */
export function ProjectMemoryDrawer({
  backendUrl,
  authHeaders,
  projectId,
  projectName,
  onClose,
}: {
  backendUrl: string;
  authHeaders: AuthHeaders;
  projectId: string;
  projectName: string;
  onClose: () => void;
}) {
  const [facts, setFacts] = useState<MemoryFact[]>([]);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const authRef = useRef(authHeaders);
  authRef.current = authHeaders;
  const closeRef = useRef<HTMLButtonElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  const load = useCallback(
    async (q: string) => {
      setLoading(true);
      try {
        setFacts(await getProjectMemory(backendUrl, authRef.current, projectId, q));
        setError(null);
      } catch (err: any) {
        setError(err?.message || "Project memory could not be loaded.");
      } finally {
        setLoading(false);
      }
    },
    [backendUrl, projectId]
  );

  useEffect(() => {
    void load("");
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCloseRef.current();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [load]);

  const invalidate = async (f: MemoryFact) => {
    setBusyId(f.id);
    try {
      await patchFact(backendUrl, authHeaders, projectId, f.id, { invalid: true });
      setFacts((list) => list.map((x) => (x.id === f.id ? { ...x, invalid: true, invalid_at: x.invalid_at || new Date().toISOString() } : x)));
    } catch (err: any) {
      setError(err?.message || "The fact could not be invalidated.");
    } finally {
      setBusyId(null);
    }
  };

  const remove = async (f: MemoryFact) => {
    if (!window.confirm("Delete this fact permanently? Invalidating keeps its history instead.")) return;
    setBusyId(f.id);
    try {
      await deleteFact(backendUrl, authHeaders, projectId, f.id);
      setFacts((list) => list.filter((x) => x.id !== f.id));
    } catch (err: any) {
      setError(err?.message || "The fact could not be deleted.");
    } finally {
      setBusyId(null);
    }
  };

  return (
    <>
      <div
        style={{
          position: "fixed",
          inset: 0,
          zIndex: 79,
          backgroundColor: "rgba(3, 4, 12, 0.65)",
          backdropFilter: "blur(6px)",
          WebkitBackdropFilter: "blur(6px)",
          animation: "modalFadeIn 0.15s ease-out forwards",
        }}
        onClick={onClose}
        aria-hidden="true"
      />
      <div className={styles.drawer} role="dialog" aria-modal="true" aria-label={`Project memory: ${projectName}`}>
      <div className={styles.drawerHead}>
        <div style={{ minWidth: 0 }}>
          <div className={styles.title}>Project memory</div>
          <div className={`${styles.meta} ${styles.truncate}`}>{projectName}</div>
        </div>
        <button ref={closeRef} type="button" className={styles.btn} onClick={onClose} aria-label="Close project memory">
          <X style={{ width: 13, height: 13 }} aria-hidden="true" />
        </button>
      </div>
      <div className={styles.drawerBody}>
        <form
          className={styles.steerRow}
          onSubmit={(e) => {
            e.preventDefault();
            void load(query);
          }}
        >
          <input className={styles.input} value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search facts (e.g. EGFR)" aria-label="Search project facts" />
          <button type="submit" className={styles.btn}>
            <Search style={{ width: 12, height: 12 }} aria-hidden="true" /> Search
          </button>
        </form>
        {error && (
          <p className={styles.errorText} role="alert">
            {error}
          </p>
        )}
        {loading ? (
          <p className={styles.muted}>Loading…</p>
        ) : facts.length === 0 ? (
          <p className={styles.muted}>No facts{query.trim() ? " match this search" : " saved in this project yet"}.</p>
        ) : (
          facts.map((f) => {
            const invalid = !!(f.invalid || f.invalid_at);
            return (
              <div key={f.id} className={styles.itemBox}>
                <div className={invalid ? styles.toggleOff : undefined}>{factText(f)}</div>
                <div className={styles.row} style={{ marginTop: 6, flexWrap: "wrap" }}>
                  {invalid && <Pill tone="faint">superseded{f.invalid_at ? ` ${dateText(f.invalid_at)}` : ""}</Pill>}
                  {Array.isArray(f.tags) && f.tags.includes("unverified") && <Pill tone="warn">unverified</Pill>}
                  <span className={`${styles.meta} ${styles.rowGrow}`}>
                    {[isNum(f.confidence) ? `confidence ${fmt(f.confidence, 2)}` : "", sourceText(f.source), f.valid_from ? `since ${dateText(f.valid_from)}` : ""]
                      .filter(Boolean)
                      .join(" · ")}
                  </span>
                  {!invalid && (
                    <button type="button" className={styles.btn} disabled={busyId === f.id} onClick={() => invalidate(f)} title="Mark as no longer true (history is kept)">
                      <Ban style={{ width: 12, height: 12 }} aria-hidden="true" /> Invalidate
                    </button>
                  )}
                  <button type="button" className={`${styles.btn} ${styles.btnDanger}`} disabled={busyId === f.id} onClick={() => remove(f)} aria-label="Delete fact">
                    <Trash2 style={{ width: 12, height: 12 }} aria-hidden="true" />
                  </button>
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  </>
  );
}
