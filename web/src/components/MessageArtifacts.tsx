"use client";

import React from "react";
import type { ExecutionArtifacts, TaskOutcome } from "@/types/chat";
import { artifactDisclosure } from "@/lib/artifactDisclosure";
import { ArtifactRenderFallback, ErrorBoundary } from "@/components/ErrorBoundary";
import { DiseaseTargetsArtifact } from "@/components/DiseaseTargetsArtifact";
import { DrugSafetyArtifact } from "@/components/DrugSafetyArtifact";
import { EvidenceSources } from "@/components/EvidenceSources";
import { PrimeKGArtifact } from "@/components/PrimeKGArtifact";
import { SimulationArtifact } from "@/components/SimulationArtifact";
import { SingleCellArtifact } from "@/components/SingleCellArtifact";
import { TrialLandscapeArtifact } from "@/components/TrialLandscapeArtifact";
import { VariantArtifact } from "@/components/VariantArtifact";
import { AgentDialogueArtifact } from "@/components/agentic/AgentDialogueArtifact";
import { AgentRunPanel } from "@/components/agentic/AgentRunPanel";
import { CalibrationArtifact } from "@/components/agentic/CalibrationArtifact";
import { ClaimConfidenceArtifact } from "@/components/agentic/ClaimConfidenceArtifact";
import { EvidenceConflictsArtifact } from "@/components/agentic/EvidenceConflictsArtifact";
import { HypothesesArtifact } from "@/components/agentic/HypothesesArtifact";
import { OmicsArtifact } from "@/components/agentic/OmicsArtifact";
import { PlanGraphArtifact } from "@/components/agentic/PlanGraphArtifact";
import { PlanApprovalArtifact, ProjectMemoryArtifact } from "@/components/agentic/ProjectMemoryArtifact";
import { SandboxAnalysisArtifact } from "@/components/agentic/SandboxAnalysisArtifact";
import { StructureArtifact } from "@/components/agentic/StructureArtifact";
import { CardDisclosureContext } from "@/components/agentic/ui";

/**
 * Each card has its own boundary, so one faulty artifact never takes down the conversation or its sibling cards; a
 * card that throws is replaced by a visible "could not be displayed" note with the raw data (UI-006, UI-001).
 */
function ArtifactBoundary({
  name,
  data,
  open,
  children,
}: {
  name: string;
  data?: unknown;
  /** Default open state from the disclosure policy (lib/artifactDisclosure). */
  open?: boolean;
  children: React.ReactNode;
}) {
  return (
    <ErrorBoundary name={`Artifact "${name}"`} resetKey={data} fallback={<ArtifactRenderFallback label={name.replace(/_/g, " ")} data={data} />}>
      <CardDisclosureContext.Provider value={open}>{children}</CardDisclosureContext.Provider>
    </ErrorBoundary>
  );
}

/**
 * Structured result cards shown under an answer: decisions first, then evidence, analyses, trust checks, and the run
 * itself. Which cards start open comes from the question, the task outcome and each artifact's state (artifactDisclosure).
 */
export function MessageArtifacts({
  artifacts,
  question,
  taskStatus,
}: {
  artifacts?: ExecutionArtifacts;
  question?: string | null;
  taskStatus?: TaskOutcome | null;
}) {
  if (!artifacts) return null;
  const open = artifactDisclosure({ question, taskStatus, artifacts });
  return (
    <>
      {/* Needs the reader's decision */}
      {artifacts.plan_approval && (
        <ArtifactBoundary name="plan_approval" open={open["plan_approval"]} data={artifacts.plan_approval}>
          <PlanApprovalArtifact data={artifacts.plan_approval} />
        </ArtifactBoundary>
      )}
      {/* Evidence the answer rests on */}
      {artifacts.clinical_trials && (
        <ArtifactBoundary name="clinical_trials" open={open["clinical_trials"]} data={artifacts.clinical_trials}>
          <TrialLandscapeArtifact data={artifacts.clinical_trials} />
        </ArtifactBoundary>
      )}
      {artifacts.drug_intel && (
        <ArtifactBoundary name="drug_intel" open={open["drug_intel"]} data={artifacts.drug_intel}>
          <DrugSafetyArtifact data={artifacts.drug_intel} />
        </ArtifactBoundary>
      )}
      {artifacts.variant_intel && (
        <ArtifactBoundary name="variant_intel" open={open["variant_intel"]} data={artifacts.variant_intel}>
          <VariantArtifact data={artifacts.variant_intel} />
        </ArtifactBoundary>
      )}
      {artifacts.disease_targets && (
        <ArtifactBoundary name="disease_targets" open={open["disease_targets"]} data={artifacts.disease_targets}>
          <DiseaseTargetsArtifact data={artifacts.disease_targets} />
        </ArtifactBoundary>
      )}
      {artifacts.primekg && (
        <ArtifactBoundary name="primekg" open={open["primekg"]} data={artifacts.primekg}>
          <PrimeKGArtifact data={artifacts.primekg} />
        </ArtifactBoundary>
      )}
      {artifacts.literature && (
        <ArtifactBoundary name="literature" open={open["literature"]} data={artifacts.literature}>
          <EvidenceSources data={artifacts.literature} />
        </ArtifactBoundary>
      )}
      {/* Analyses run for this question */}
      {artifacts.vivarix && (
        <ArtifactBoundary name="vivarix" open={open["vivarix"]} data={artifacts.vivarix}>
          <SimulationArtifact data={artifacts.vivarix} />
        </ArtifactBoundary>
      )}
      {artifacts.vivarix_scan && (
        <ArtifactBoundary name="vivarix_scan" open={open["vivarix_scan"]} data={artifacts.vivarix_scan}>
          <SimulationArtifact data={artifacts.vivarix_scan} />
        </ArtifactBoundary>
      )}
      {artifacts.structure_intel && (
        <ArtifactBoundary name="structure_intel" open={open["structure_intel"]} data={artifacts.structure_intel}>
          <StructureArtifact data={artifacts.structure_intel} />
        </ArtifactBoundary>
      )}
      {artifacts.omics_intel && (
        <ArtifactBoundary name="omics_intel" open={open["omics_intel"]} data={artifacts.omics_intel}>
          <OmicsArtifact data={artifacts.omics_intel} />
        </ArtifactBoundary>
      )}
      {artifacts.lumicell && (
        <ArtifactBoundary name="lumicell" open={open["lumicell"]} data={artifacts.lumicell}>
          <SingleCellArtifact data={artifacts.lumicell} datasets={artifacts.datasets} />
        </ArtifactBoundary>
      )}
      {artifacts.sandbox_analysis && (
        <ArtifactBoundary name="sandbox_analysis" open={open["sandbox_analysis"]} data={artifacts.sandbox_analysis}>
          <SandboxAnalysisArtifact data={artifacts.sandbox_analysis} />
        </ArtifactBoundary>
      )}
      {artifacts.model_calibration && (
        <ArtifactBoundary name="model_calibration" open={open["model_calibration"]} data={artifacts.model_calibration}>
          <CalibrationArtifact data={artifacts.model_calibration} />
        </ArtifactBoundary>
      )}
      {artifacts.hypotheses && (
        <ArtifactBoundary name="hypotheses" open={open["hypotheses"]} data={artifacts.hypotheses}>
          <HypothesesArtifact data={artifacts.hypotheses} />
        </ArtifactBoundary>
      )}
      {/* How far the answer can be trusted */}
      {artifacts.evidence_conflicts && (
        <ArtifactBoundary name="evidence_conflicts" open={open["evidence_conflicts"]} data={artifacts.evidence_conflicts}>
          <EvidenceConflictsArtifact data={artifacts.evidence_conflicts} />
        </ArtifactBoundary>
      )}
      {artifacts.claim_confidence && (
        <ArtifactBoundary name="claim_confidence" open={open["claim_confidence"]} data={artifacts.claim_confidence}>
          <ClaimConfidenceArtifact data={artifacts.claim_confidence} />
        </ArtifactBoundary>
      )}
      {/* How the agent worked */}
      {artifacts.agent_run && (
        <ArtifactBoundary name="agent_run" open={open["agent_run"]} data={artifacts.agent_run}>
          <AgentRunPanel data={artifacts.agent_run} routing={artifacts.framework_routing} />
        </ArtifactBoundary>
      )}
      {artifacts.agent_plan && (
        <ArtifactBoundary name="agent_plan" open={open["agent_plan"]} data={artifacts.agent_plan}>
          <PlanGraphArtifact data={artifacts.agent_plan} />
        </ArtifactBoundary>
      )}
      {artifacts.agent_dialogue && (
        <ArtifactBoundary name="agent_dialogue" open={open["agent_dialogue"]} data={artifacts.agent_dialogue}>
          <AgentDialogueArtifact data={artifacts.agent_dialogue} />
        </ArtifactBoundary>
      )}
      {artifacts.project_memory && (
        <ArtifactBoundary name="project_memory" open={open["project_memory"]} data={artifacts.project_memory}>
          <ProjectMemoryArtifact data={artifacts.project_memory} />
        </ArtifactBoundary>
      )}
    </>
  );
}
