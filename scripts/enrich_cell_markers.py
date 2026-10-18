"""
Enrichment script for human cell marker atlas in Phronesis Lumicell.
Expands the reference marker atlas across 300+ canonical human cell types,
drawing consensus markers from CellMarker 2.0, PanglaoDB, Azimuth, Tabula Sapiens,
and Human Cell Atlas (HCA) benchmarks.
"""

import json
import re
from pathlib import Path

MARKERS_PATH = Path("phronesis/lumicell/markers/cell_markers.json")

# Supplementary canonical markers
SUPPLEMENTARY_MARKERS = {
    # ---------------------------------------------------------
    # 1. Innate Lymphoid Cells (ILC) & Unconventional T
    # ---------------------------------------------------------
    "Innate Lymphoid Cells (ILC1)": ["TBX21", "IFNG", "IKZF3", "NCR1", "IL7R", "CD2"],
    "Innate Lymphoid Cells (ILC2)": ["GATA3", "IL1RL1", "PTGDR2", "AREG", "IL7R", "KIT"],
    "Innate Lymphoid Cells (ILC3)": ["RORC", "IL23R", "KIT", "IL22", "IL7R", "AHR"],
    "Lymphoid Tissue Inducer Cells (LTi)": ["RORC", "LTB", "LTA", "KIT", "IL7R", "TNFSF11"],
    "Gamma-Delta T Cells (Vd2 / Vg9Vd2)": ["TRDV2", "TRGV9", "CD3D", "GZMB", "NKG7", "KLRD1"],
    "T Cells (Stem Cell Memory / Tscm)": ["TCF7", "LEF1", "IL7R", "CCR7", "SELL", "CD27", "FAS"],
    "T Cells (CD8+ Tissue-Resident Memory / Trm)": ["CD8A", "ITGAE", "CD69", "ZNF683", "PRDM1", "CXCR6"],
    "T Cells (Effector Memory CD45RA+ / TEMRA)": ["CD8A", "B3GAT1", "FGFBP2", "CX3CR1", "GZMB", "PRF1"],
    "T Peripheral Helper Cells (Tph)": ["PDCD1", "CXCL13", "MAF", "IL21", "CD4", "TIGIT"],
    "Proliferating / Cycling T Cells": ["MKI67", "TOP2A", "CDK1", "PCNA", "STMN1", "TYMS"],
    "Thymocytes (Double-Positive CD4+ CD8+)": ["CD4", "CD8A", "CD8B", "RAG1", "RAG2", "PTCRA", "CD1A"],
    "Thymocytes (Early Double-Negative)": ["PTCRA", "NOTCH1", "HES1", "CD34", "IL7R"],

    # ---------------------------------------------------------
    # 2. B Cell Granular Sublineages
    # ---------------------------------------------------------
    "B Cells (Transitional)": ["CD24", "CD38", "IGHD", "IGHM", "CD9", "IL4R"],
    "B Cells (Follicular / Mantle)": ["CD19", "MS4A1", "CD22", "FCER2", "CXCR5", "IGHD"],
    "B Cells (Marginal Zone)": ["CD19", "CD1C", "CD27", "CR2", "IGHM", "NOTCH2"],
    "Atypical / Age-Associated B Cells (ABCs)": ["ITGAX", "TBX21", "FCRL5", "ZEB2", "AIM2"],
    "Bone Marrow Long-Lived Plasma Cells": ["SDC1", "TNFRSF17", "SLAMF7", "PRDM1", "XBP1", "CXCR4"],

    # ---------------------------------------------------------
    # 3. Myeloid, Histiocytic & Antigen-Presenting Specialty
    # ---------------------------------------------------------
    "Dendritic Cells (AS-DC / Pre-DC)": ["AXL", "SIGLEC6", "CD22", "LILRA4", "CXCR3"],
    "Monocyte-Derived Dendritic Cells (moDC)": ["CD1A", "CD1C", "FCER1A", "ITGAX", "CD209"],
    "Langerhans Cells (Epidermal DC)": ["CD207", "CD1A", "EPCAM", "ITGAX", "CLDN1"],
    "Splenic Red Pulp Macrophages": ["VCAM1", "SPIC", "HMOX1", "CD68", "CD163"],
    "Perivascular Macrophages (Brain PVM)": ["LYVE1", "CD163", "MRC1", "STAB1", "CD68"],
    "Lipid-Associated Macrophages (LAM)": ["TREM2", "APOE", "LPL", "SPP1", "FABP4", "CD9"],
    "Fibrotic / Scar-Associated Macrophages": ["SPP1", "MMP9", "TREM2", "LGALS3", "CTSB"],
    "Myeloid-Derived Suppressor Cells (MDSC)": ["S100A8", "S100A9", "ARG1", "CD33", "ITGAM", "IL4R"],
    "Circulating Monocyte Progenitors (cMoP)": ["CD34", "KIT", "CSF1R", "FLT3", "IRF8"],

    # ---------------------------------------------------------
    # 4. Central & Peripheral Nervous System Neurons
    # ---------------------------------------------------------
    "Cortical Excitatory Neurons (Layer 2/3)": ["CUX2", "LAMP5", "GLRA3", "CARTPT", "SLC17A7"],
    "Cortical Excitatory Neurons (Layer 4)": ["RORB", "COL5A2", "ETV1", "SLC17A7"],
    "Cortical Excitatory Neurons (Layer 5 ET/IT)": ["BCL6", "FEZF2", "POU3F2", "SLC17A7", "ETV1"],
    "Cortical Excitatory Neurons (Layer 6 CT)": ["FOXP2", "TLE4", "SYNPR", "CRYM", "SLC17A7"],
    "GABAergic Interneurons (Pvalb+ Basket/Chandelier)": ["PVALB", "TAC1", "LHX6", "GAD1", "GAD2"],
    "GABAergic Interneurons (Sst+ Martinotti)": ["SST", "NPY", "GRM1", "CALB1", "GAD1"],
    "GABAergic Interneurons (Vip+ Disinhibitory)": ["VIP", "CALB2", "CRH", "HTR3A", "GAD1"],
    "GABAergic Interneurons (Lamp5+ Neurogliaform)": ["LAMP5", "NDNF", "RELN", "KIT", "GAD1"],
    "Cerebellar Purkinje Cells": ["PCP4", "CALB1", "ITPR1", "CA8", "GRID2"],
    "Cerebellar Granule Cells": ["GABRA6", "FAT2", "RBFOX3", "PAX6", "RELN"],
    "Spinal Cord Motor Neurons": ["CHAT", "ISL1", "MNX1", "SLC5A7", "SLC18A3"],
    "Striatal Spiny Projection Neurons (D1 Direct)": ["DRD1", "TAC1", "PDYN", "EBF1", "PPP1R1B"],
    "Striatal Spiny Projection Neurons (D2 Indirect)": ["DRD2", "PENK", "ADORA2A", "GPR6", "PPP1R1B"],
    "Striatal Cholinergic Interneurons (ChI)": ["CHAT", "SLC18A3", "SLC5A7", "ISL1", "ERBB4"],
    "Substantia Nigra Dopaminergic Neurons": ["TH", "SLC6A3", "ALDH1A1", "KCNJ6", "FOXA2"],
    "Ventral Tegmental Dopaminergic Neurons": ["TH", "SLC6A3", "CALB1", "OTX2", "SLC17A6"],
    "Locus Coeruleus Noradrenergic Neurons": ["DBH", "SLC6A2", "TH", "PHOX2B", "SLC18A2"],
    "Hypothalamic POMC Neurons (Anorexigenic)": ["POMC", "LEPR", "MC4R", "CARTPT", "PCSK1"],
    "Hypothalamic AgRP / NPY Neurons (Orexigenic)": ["AGRP", "NPY", "LEPR", "GHSR", "SLC32A1"],
    "Hypothalamic Oxytocin Neurons": ["OXT", "AVP", "SLC17A6", "SCG2"],
    "Hypothalamic Vasopressin Neurons": ["AVP", "OXT", "SLC17A6"],
    "Dorsal Root Ganglion Nociceptors (Peptidergic)": ["TAC1", "CALCA", "TRPV1", "SCN10A", "NTRK1"],
    "Dorsal Root Ganglion Nociceptors (Non-peptidergic)": ["P2RX3", "MRGPRD", "RET", "SCN10A"],
    "Dorsal Root Ganglion Mechanoreceptors": ["NTRK2", "NTRK3", "PIEZO2", "PVALB", "NEFH"],

    # ---------------------------------------------------------
    # 5. Glial, Ependymal & Neuro-Stroma
    # ---------------------------------------------------------
    "Bergmann Glia (Cerebellum)": ["GDF10", "SLC1A3", "FABP7", "S100B", "GLI1"],
    "Tanycytes (Hypothalamic)": ["RAX", "VIM", "NES", "DIO2", "COL23A1"],
    "Meningeal Fibroblasts / Leptomeningeal": ["LUM", "DCN", "COL1A1", "SLC6A13", "FOXC1"],
    "Non-Myelinating / Remak Schwann Cells": ["NCAM1", "L1CAM", "NGFR", "SOX10", "S100B"],

    # ---------------------------------------------------------
    # 6. Sensory Organs (Ocular, Auditory, Gustatory, Olfactory)
    # ---------------------------------------------------------
    "Retinal Horizontal Cells": ["LHX1", "ONECUT1", "CALB1", "TFAP2A"],
    "Retinal Amacrine Cells": ["PAX6", "TFAP2B", "GAD1", "SLC6A9", "CHAT"],
    "Retinal Bipolar Cells": ["VSX2", "OTX2", "TRPM1", "GRM6", "CABP5"],
    "Retinal Pigment Epithelium (RPE)": ["RPE65", "BEST1", "TYR", "RLBP1", "MITF"],
    "Retinal Muller Glia": ["RLBP1", "SLC1A3", "GLUL", "APOE", "VIM"],
    "Corneal Epithelial Cells": ["KRT12", "KRT3", "PAX6", "TP63", "CLDN7"],
    "Corneal Endothelial Cells": ["COL8A2", "SLC4A11", "ATP1A1", "ALCAM"],
    "Corneal Keratocytes (Stromal)": ["KERA", "LUM", "ALDH3A1", "ALDH1A1", "COL1A1"],
    "Trabecular Meshwork Cells": ["MYOC", "TIMP3", "MGP", "CHI3L1", "AQP1"],
    "Lens Fiber Cells": ["CRYAA", "CRYAB", "CRYBB1", "BFSP1", "MIP"],
    "Cochlear Hair Cells (Auditory)": ["MYO7A", "ATOH1", "POU4F3", "SLC26A5", "PCP4"],
    "Cochlear Supporting Cells (Deiters / Pillar)": ["SOX2", "FGFR3", "S100B", "GJB2", "GJB6"],
    "Stria Vascularis Marginal Cells": ["KCNQ1", "KCNE1", "ATP1A1", "SLC12A2"],
    "Olfactory Sensory Neurons": ["OMP", "CNGA2", "STMN2", "GAP43", "GNG13"],
    "Olfactory Ensheathing Cells": ["S100B", "SOX10", "NGFR", "GFAP", "NCAM1"],
    "Taste Receptor Cells": ["TRPM5", "TAS1R3", "GNAT3", "PLCB2", "SNAP25"],

    # ---------------------------------------------------------
    # 7. Respiratory Subtypes & Submucosal Lineages
    # ---------------------------------------------------------
    "Airway Basal Stem Cells": ["KRT5", "TP63", "KRT14", "NGFR", "LAMB3"],
    "Pulmonary Neuroendocrine Cells (PNEC)": ["CHGA", "SYP", "CALCA", "GRP", "NCAM1"],
    "Pre-Alveolar Type 1 Transition Cells (DATP / PAT)": ["KRT8", "CLDN4", "FN1", "CDKN1A", "TACSTD2"],
    "Nasal Epithelial Brush / Tuft Cells": ["TRPM5", "LRMP", "IL25", "POU2F3", "ASCL3"],
    "Bronchial Submucosal Gland Mucous Cells": ["MUC5B", "MUC5AC", "TFF3", "AGR2"],
    "Bronchial Submucosal Gland Serous Cells": ["LTF", "LYZ", "PRR4", "PIP", "AZGP1"],

    # ---------------------------------------------------------
    # 8. Gastrointestinal & Enteroendocrine Subtypes
    # ---------------------------------------------------------
    "Esophageal Stratified Squamous Epithelium": ["KRT13", "KRT4", "TP63", "DSG1", "SPRR3"],
    "Gastric Surface Mucous / Pit Cells": ["MUC5AC", "TFF1", "GKN1", "GKN2"],
    "Gastric Mucous Neck Cells": ["MUC6", "TFF2", "GKN1", "PGC"],
    "Gastric G Cells (Gastrin)": ["GAST", "CHGA", "ISL1", "PCSK1"],
    "Gastric Enterochromaffin-like Cells (ECL)": ["HDC", "CHGA", "SLC18A2"],
    "Enteroendocrine I Cells (CCK)": ["CCK", "CHGA", "NEUROD1", "PCSK1"],
    "Enteroendocrine S Cells (Secretin)": ["SCT", "CHGA", "ISL1", "PCSK1"],
    "Enteroendocrine K Cells (GIP)": ["GIP", "CHGA", "NEUROD1", "PCSK1"],
    "Enteroendocrine L Cells (GLP-1 / PYY)": ["GCG", "PYY", "CHGA", "NEUROG3"],
    "Enterochromaffin Cells (Serotonin / 5-HT)": ["TPH1", "CHGA", "TAC1", "SLC18A1"],
    "Microfold Cells (M Cells)": ["GP2", "SPIB", "CCL9", "MARCKSL1", "SOX8"],
    "Colonic Colonocytes (Absorptive)": ["CA1", "CA2", "SLC26A3", "AQP8", "CEACAM5"],
    "Colonic Deep Crypt Secretory Cells": ["REG4", "LYZ", "SOX9", "MUC2"],
    "Interstitial Cells of Cajal (ICC)": ["KIT", "ANO1", "ETV1", "PRKG1"],
    "Duodenal Brunner Gland Mucous Cells": ["MUC6", "TFF2", "BPIFB1", "CHGA"],

    # ---------------------------------------------------------
    # 9. Liver, Biliary & Gallbladder Granular
    # ---------------------------------------------------------
    "Periportal Hepatocytes (Zone 1)": ["ALB", "HAL", "ARG1", "ASS1", "PCK1", "CPS1"],
    "Perivenous Hepatocytes (Zone 3)": ["CYP2E1", "CYP1A2", "GLUL", "OAT", "SLCO1B1"],
    "Gallbladder Epithelial Cells": ["MUC5B", "KRT19", "CFTR", "EPCAM", "ANXA10"],

    # ---------------------------------------------------------
    # 10. Kidney Nephron & Collecting Duct Segments
    # ---------------------------------------------------------
    "Kidney Parietal Epithelial Cells": ["PAX8", "PAX2", "CLDN1", "CD44", "VCAM1"],
    "Kidney Juxtaglomerular Renin Cells": ["REN", "AKR1D1", "MYH11", "CALB1"],
    "Kidney Macula Densa Cells": ["NOS1", "SLC12A1", "PTGS2", "GATA3"],
    "Kidney Proximal Tubule (S1/S2 Early)": ["SLC5A2", "LRP2", "CUBN", "SLC34A1", "ALDOB"],
    "Kidney Proximal Tubule (S3 Late)": ["SLC22A6", "SLC22A8", "SLC7A13", "CYP4A11"],
    "Kidney Descending Thin Limb": ["AQP1", "CLDN2", "BST1", "TACSTD2"],
    "Kidney Ascending Thin Limb": ["CLCNKA", "CLCNKB", "BSND", "TACSTD2"],
    "Kidney Connecting Tubule (CNT)": ["CALB1", "SLC8A1", "TRPV5", "KL"],
    "Kidney Medullary Interstitial Cells": ["PTGDS", "COL1A1", "LUM", "MMP2"],

    # ---------------------------------------------------------
    # 11. Pancreas Endocrine & Centroacinar
    # ---------------------------------------------------------
    "Pancreatic Ghrelin / Epsilon Cells": ["GHRL", "CHGA", "ARX", "PCSK1"],
    "Pancreatic Centroacinar Cells": ["HES1", "CA2", "ALDH1A1", "CFTR", "KRT19"],

    # ---------------------------------------------------------
    # 12. Cardiovascular, Vascular & Stroma
    # ---------------------------------------------------------
    "Endocardial Cells": ["NPR3", "EMCN", "NFATC1", "PECAM1", "POSTN"],
    "Cardiac Fibroblasts": ["COL1A1", "COL1A2", "DCN", "TCF21", "POSTN"],
    "Cardiac Valve Interstitial Cells (VIC)": ["POSTN", "TWIST1", "COL1A1", "VCAN", "ACTA2"],
    "Cardiac Valve Endothelial Cells (VEC)": ["CDH5", "PECAM1", "NOS3", "NFATC1", "CD44"],
    "Epicardial Cells": ["WT1", "TBX18", "TCF21", "BNC1", "MSLN"],
    "Cardiac Telocytes": ["CD34", "PDGFRB", "VIM", "KIT", "PDGFRA"],
    "Coronary Vascular Endothelial Cells": ["FABP4", "CD36", "FLT1", "PECAM1", "CDH5"],
    "High Endothelial Venules (HEV)": ["CHST4", "MADCAM1", "SELL", "CCL21", "ACKR1"],
    "Splenic Sinusoidal Endothelial Cells": ["STAB2", "CD8A", "CLEC4G", "PECAM1"],
    "Bone Marrow Sinusoidal Endothelial Cells": ["SELE", "VCAM1", "KITLG", "NOS3", "FLT4"],
    "Bone Marrow CXCL12-Abundant Reticular (CAR)": ["LEPR", "CXCL12", "KITLG", "PDGFRB"],

    # ---------------------------------------------------------
    # 13. Hematopoietic Progenitors
    # ---------------------------------------------------------
    "Common Lymphoid Progenitors (CLP)": ["IL7R", "CD38", "FLT3", "KIT", "CD34"],
    "Common Myeloid Progenitors (CMP)": ["CD34", "CD38", "KIT", "IL3RA", "ITGA4"],
    "Megakaryocyte-Erythroid Progenitors (MEP)": ["CD34", "KIT", "GATA1", "TAL1", "KLF1"],
    "Granulocyte-Monocyte Progenitors (GMP)": ["CD34", "KIT", "MPO", "CEBPA", "CSF3R"],

    # ---------------------------------------------------------
    # 14. Pituitary & Parathyroid Endocrine
    # ---------------------------------------------------------
    "Pituitary Somatotropes (GH)": ["GH1", "POU1F1", "GHRHR", "CHGA"],
    "Pituitary Lactotropes (PRL)": ["PRL", "POU1F1", "DRD2", "CHGA"],
    "Pituitary Corticotropes (ACTH)": ["POMC", "TBX19", "CRHR1", "CHGA"],
    "Pituitary Thyrotropes (TSH)": ["TSHB", "POU1F1", "TRHR", "CHGA"],
    "Pituitary Gonadotropes (LH/FSH)": ["LHB", "FSHB", "NR5A1", "GNRHR"],
    "Parathyroid Chief Cells": ["PTH", "CASR", "GCM2", "MAFB"],
    "Parathyroid Oxyphil Cells": ["OXCT1", "NDUFA1", "ATP5F1A", "CASR", "PTH"],

    # ---------------------------------------------------------
    # 15. Adrenal Zonal Subtypes
    # ---------------------------------------------------------
    "Adrenal Zona Glomerulosa": ["CYP11B2", "NR4A2", "VSTM2A", "STAR", "NR0B1"],
    "Adrenal Zona Fasciculata": ["CYP11B1", "CYP17A1", "MC2R", "STAR", "FDX1"],
    "Adrenal Zona Reticularis": ["SULT2A1", "CYP17A1", "FDX1", "CYB5A"],

    # ---------------------------------------------------------
    # 16. Mammary Gland Subtypes
    # ---------------------------------------------------------
    "Mammary Luminal Epithelial Cells": ["KRT18", "KRT19", "EPCAM", "ESR1", "PGR", "MUC1"],
    "Mammary Myoepithelial / Basal Cells": ["KRT5", "KRT14", "ACTA2", "TP63", "TAGLN"],
    "Mammary Progenitor Cells": ["ALDH1A1", "KIT", "CD44", "EPCAM", "PROM1"],

    # ---------------------------------------------------------
    # 17. Reproductive Organs (Male & Female)
    # ---------------------------------------------------------
    "Fallopian Tube Ciliated Epithelium": ["FOXJ1", "CAPS", "TUBB4B", "PAX8"],
    "Fallopian Tube Secretory Epithelium": ["OVGP1", "PAX8", "KRT7", "CD44"],
    "Endometrial Stromal Cells (Decidualized)": ["HAND2", "IGFBP1", "PRL", "HOXA10", "FOXO1"],
    "Endometrial Epithelial Cells (Glandular)": ["PAX8", "MUC1", "FOXA2", "LGR5", "SOX9"],
    "Ovarian Surface Epithelial Cells": ["KRT7", "KRT19", "PAX8", "EPCAM", "WT1"],
    "Corpus Luteum Luteal Cells": ["STAR", "CYP11A1", "HSD3B2", "LHCGR", "VEGFA"],
    "Uterine Natural Killer Cells (uNK)": ["NCAM1", "KIR2DL1", "KIR2DL4", "ITGA1", "SPINK2"],
    "Cervical Squamous Epithelial Cells": ["KRT5", "KRT14", "TP63", "CD44", "DSG3"],
    "Cervical Endocervical Columnar Cells": ["MUC5B", "MUC5AC", "KRT7", "AGR2"],
    "Prostate Luminal Epithelial Cells": ["KLK3", "AR", "ACPP", "MSMB", "NKX3-1"],
    "Prostate Basal Epithelial Cells": ["KRT5", "KRT14", "TP63", "GSTP1"],
    "Prostate Neuroendocrine Cells": ["CHGA", "SYP", "SCG2", "ENO2"],
    "Seminal Vesicle Epithelial Cells": ["SEMG1", "SEMG2", "TGM4", "AR"],
    "Epididymal Principal Cells": ["DEFB106A", "CRISP1", "AQP9", "GSTM2"],
    "Testicular Peritubular Myoid Cells": ["ACTA2", "MYH11", "TAGLN", "DES", "AR"],

    # ---------------------------------------------------------
    # 18. Skin, Appendage & Connective Specialty
    # ---------------------------------------------------------
    "Sebocytes (Sebaceous Glands)": ["SCD", "FADS2", "PPARG", "MGST1", "KRT79"],
    "Eccrine Sweat Gland Epithelial Cells": ["KRT19", "AQP5", "CHRM3", "SCGB1D2"],
    "Hair Follicle Stem Cells (Bulge)": ["CD34", "LGR5", "SOX9", "NFATC1", "KRT15"],
    "Dermal Papilla Cells": ["ALPL", "VCAN", "CORIN", "LEF1", "SOX2"],
    "Merkel Cells (Mechanoreceptors)": ["KRT20", "ATOH1", "INSM1", "CHGA", "SOX2"],
    "Adipose Progenitor Cells (Preadipocytes)": ["PDGFRA", "CD34", "DPP4", "F3", "FABP4"],
    "Tenocytes (Tendon / Ligament)": ["SCX", "MKX", "TNMD", "COL1A1", "THBS4"],
    "Synoviocytes (Fibroblast-like / FLS)": ["PRG4", "THY1", "CD55", "FN1", "PDPN"],
    "Synoviocytes (Macrophage-like / MLS)": ["CD68", "CD163", "MARCO", "AIF1", "C1QA"],

    # ---------------------------------------------------------
    # 19. Lymphoid Stroma & Thymic Microenvironment
    # ---------------------------------------------------------
    "Thymic Epithelial Cells (Cortical / cTEC)": ["PSMB11", "PRSS16", "TBATA", "CXCL12", "EPCAM"],
    "Thymic Epithelial Cells (Medullary / mTEC)": ["AIRE", "FEZF2", "TNFRSF11A", "KRT5", "CLDN4"],
    "Thymic Tuft Cells": ["POU2F3", "TRPM5", "IL25", "CHAT", "AVIL"],
    "Follicular Dendritic Cells (FDC)": ["CR2", "CR1", "CXCL13", "FCAMR", "MFGE8"],
    "Fibroblastic Reticular Cells (FRC)": ["CCL19", "CCL21", "PDPN", "IL7", "VCAM1"],
    "Marginal Reticular Cells (MRC)": ["MADCAM1", "TNFSF11", "CXCL13", "PDPN"],

    # ---------------------------------------------------------
    # 20. Placenta & Embryonic / Fetal Specialty
    # ---------------------------------------------------------
    "Villous Cytotrophoblasts (CTB)": ["PAGE4", "EGFR", "TP63", "EPCAM", "GATA3"],
    "Placental Hofbauer Cells": ["CD68", "CD163", "LYVE1", "FOLR2", "MRC1"],
    "Amniotic Epithelial Cells": ["EPCAM", "KRT7", "TFAP2A", "CLDN1", "GATA3"],
    "Yolk Sac Endodermal Cells": ["AFP", "APOA1", "TTR", "GATA4", "SOX17"],
}


def main() -> None:
    if not MARKERS_PATH.exists():
        raise FileNotFoundError(f"Missing {MARKERS_PATH}")

    with open(MARKERS_PATH, "r", encoding="utf-8") as f:
        existing = json.load(f)

    # Validate all genes match uppercase HGNC symbol convention
    invalid_symbols = []
    for cell_type, markers in SUPPLEMENTARY_MARKERS.items():
        for marker in markers:
            if not re.match(r"^[A-Z0-9\-_]+$", marker):
                invalid_symbols.append((cell_type, marker))

    if invalid_symbols:
        raise ValueError(f"Found invalid symbols: {invalid_symbols}")

    merged = dict(existing)
    added_count = 0
    updated_count = 0

    for cell_type, markers in SUPPLEMENTARY_MARKERS.items():
        if cell_type not in merged:
            merged[cell_type] = markers
            added_count += 1
        else:
            prev = merged[cell_type]
            combined = list(dict.fromkeys(prev + markers))
            if len(combined) > len(prev):
                merged[cell_type] = combined
                updated_count += 1

    # Write out cleanly formatted JSON
    with open(MARKERS_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
        f.write("\n")

    total_cell_types = len(merged)
    total_markers = sum(len(v) for v in merged.values())
    unique_genes = len(set(g for v in merged.values() for g in v))

    print(f"Enrichment Complete!")
    print(f"  Existing Cell Types: {len(existing)}")
    print(f"  Newly Added Cell Types: {added_count}")
    print(f"  Updated Cell Types: {updated_count}")
    print(f"  Total Cell Types: {total_cell_types}")
    print(f"  Total Marker Associations: {total_markers}")
    print(f"  Unique Marker Genes: {unique_genes}")


if __name__ == "__main__":
    main()
