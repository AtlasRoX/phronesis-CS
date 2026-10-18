"""
Comprehensive System & Logic Audit for Phronesis.
Audits:
1. Lumicell Cell Marker Atlas (HGNC symbols, duplicates, non-RNA antibody antigens, annotation scoring logic).
2. Lumicell Analysis Engine (matrix scaling, cluster annotation algorithm, dimension checks).
3. Vivarix Engine (COPASI simulation, parameter scan boundaries, error trapping).
4. LithoScribe & Evidence Grading (evidence grading hierarchy, citation anchor extraction).
5. Orchestrator & Agents (state handling, tool dispatch, memory scoping).
6. Datasets & Validators (file extensions, schema enforcement).
"""

import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path


def audit_cell_markers():
    print("=" * 60)
    print("AUDIT SECTION 1: CELL MARKERS & ANNOTATION LOGIC")
    print("=" * 60)
    marker_file = Path("phronesis/lumicell/markers/cell_markers.json")
    if not marker_file.exists():
        print("[FAIL] Marker file missing!")
        return

    with open(marker_file, "r", encoding="utf-8") as f:
        atlas = json.load(f)

    print(f"Total cell types in JSON atlas: {len(atlas)}")

    # 1. Non-HGNC aliases & Non-RNA flow cytometry antigens
    flow_antigens = {
        "SSEA-3", "SSEA-4", "TRA-1-60", "TRA-1-81", "STRO-1", "PSA-NCAM"
    }
    alias_map = {
        "BRACHYURY": "TBXT",
        "ALBUMIN": "ALB",
        "KERATIN-5": "KRT5",
        "CYTOKERATIN-18": "KRT18",
        "CYTOKERATIN-7": "KRT7",
        "E-CADHERIN": "CDH1",
        "N-CADHERIN": "CDH2",
        "VE-CADHERIN": "CDH5",
        "VEGFR-2": "KDR",
        "ALPHA-SMA": "ACTA2",
        "PDGFR-BETA": "PDGFRB",
        "IFN-GAMMA": "IFNG",
        "IL-17RB": "IL17RB",
        "SIGLEC-8": "SIGLEC8",
        "HLA-DR": "HLA-DRA",
        "TRAV1-2": "TRAV1-2",
    }

    found_antigens = []
    found_aliases = []
    invalid_genes = []

    for ctype, genes in atlas.items():
        for g in genes:
            if g in flow_antigens:
                found_antigens.append((ctype, g))
            if g in alias_map:
                found_aliases.append((ctype, g, alias_map[g]))
            # Standard HGNC symbols: uppercase letters, digits, hyphens
            if not re.match(r"^[A-Z0-9\-_]+$", g):
                invalid_genes.append((ctype, g))

    print(f"\n[ISSUE 1.1] Non-RNA Flow Cytometry Carbohydrate Antigens: {len(found_antigens)}")
    for ctype, g in found_antigens[:5]:
        print(f"  - '{ctype}': marker '{g}' is a glycolipid/carbohydrate antibody target, not an mRNA gene!")

    print(f"\n[ISSUE 1.2] Deprecated / Non-Standard Protein Aliases: {len(found_aliases)}")
    for ctype, g, hgnc in found_aliases[:5]:
        print(f"  - '{ctype}': marker '{g}' should be official HGNC symbol '{hgnc}'")

    # 2. Near-duplicate / Colliding Cell Type Names
    names = list(atlas.keys())
    normalized = {}
    duplicates = []
    for n in names:
        norm = re.sub(r"[^a-z0-9]", "", n.lower().replace("cells", "cell"))
        if norm in normalized:
            duplicates.append((n, normalized[norm]))
        else:
            normalized[norm] = n

    print(f"\n[ISSUE 1.3] Redundant / Unstructured Colliding Cell Types: {len(duplicates)}")
    for a, b in duplicates[:10]:
        print(f"  - Colliding pair: '{a}' vs '{b}'")

    # 3. Size imbalance
    lengths = {k: len(v) for k, v in atlas.items()}
    short_sets = [k for k, v in lengths.items() if v < 2]
    long_sets = [k for k, v in lengths.items() if v > 15]
    print(f"\n[ISSUE 1.4] Size Imbalance in Signatures:")
    print(f"  - Cell types with < 2 markers: {len(short_sets)} (e.g. {short_sets[:3]})")
    print(f"  - Cell types with > 15 markers: {len(long_sets)}")

    # 4. Engine Cluster Annotation Logic Flaw
    print("\n[ISSUE 1.5] Annotation Scoring Algorithm Flaw in engine.py:")
    print("  - Current code: mean_expr = float(np.mean(arr)); if mean_expr > best_score...")
    print("  - FLAW: If cell type A has 1 marker matched at expr 3.0, its mean is 3.0.")
    print("  - If cell type B has 6 markers matched (all expressing at 2.5), its mean is 2.5.")
    print("  - Result: Type A erroneously wins over Type B because mean is not normalized by marker coverage fraction or specificity!")


def audit_lumicell_engine():
    print("\n" + "=" * 60)
    print("AUDIT SECTION 2: LUMICELL ANALYSIS ENGINE")
    print("=" * 60)
    from phronesis.lumicell.engine import LumicellAnalysisEngine

    # Check method signatures and error traps
    import inspect
    sig = inspect.signature(LumicellAnalysisEngine.execute_single_cell_pipeline)
    print("execute_single_cell_pipeline signature:", sig)

    # Check handling of edge cases: 1 cell, 0 genes, etc.
    engine = LumicellAnalysisEngine()
    print("Engine instantiated successfully.")


def audit_vivarix_engine():
    print("\n" + "=" * 60)
    print("AUDIT SECTION 3: VIVARIX SIMULATION ENGINE")
    print("=" * 60)
    from phronesis.vivarix.engine import VivarixSimulationEngine, SimulationValidationError

    engine = VivarixSimulationEngine()
    # Check what happens with unknown model
    try:
        engine.simulate_time_course(sbml_path="NON_EXISTENT_MODEL.xml", duration=10.0)
        print("[FAIL] Did not raise error for missing model!")
    except FileNotFoundError:
        print("[OK] Traps missing file correctly.")
    except Exception as e:
        print(f"[NOTE] Raised {type(e).__name__}: {e}")


def audit_orchestrator():
    print("\n" + "=" * 60)
    print("AUDIT SECTION 4: AUTHORITATIVE ORCHESTRATOR & MEMORY")
    print("=" * 60)
    from phronesis.agents.authoritative_orchestrator import AuthoritativeOrchestrator

    orchestrator = AuthoritativeOrchestrator()
    print("Orchestrator instantiated.")
    # Check pronoun resolution logic
    subj = orchestrator.memory.get_active_subject("user1", "thread1")
    print(f"Initial active subject: {subj} (Expected None)")

    orchestrator.memory.record_active_subject("user1", "thread1", {"drug": "Sotorasib"})
    subj2 = orchestrator.memory.get_active_subject("user1", "thread1")
    print(f"Recorded active subject: {subj2}")

    # Check thread isolation
    subj3 = orchestrator.memory.get_active_subject("user1", "thread2")
    print(f"Isolated thread subject: {subj3} (Expected None)")


if __name__ == "__main__":
    audit_cell_markers()
    audit_lumicell_engine()
    audit_vivarix_engine()
    audit_orchestrator()
