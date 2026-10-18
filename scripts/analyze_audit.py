import json
from collections import Counter

with open("AUDIT_EVALUATION_CASES.json", "r", encoding="utf-8") as f:
    cases = json.load(f)

print(f"Total Cases: {len(cases)}")
print("=" * 100)
categories = Counter(c["category"] for c in cases)
for cat, count in categories.items():
    print(f"  Category: {cat} ({count} cases)")

print("=" * 100)
passed_count = sum(1 for c in cases if c.get("verification_passed"))
failed_count = sum(1 for c in cases if not c.get("verification_passed"))
vivarix_count = sum(1 for c in cases if c.get("vivarix_executed"))
lumicell_count = sum(1 for c in cases if c.get("lumicell_executed"))
avg_elapsed = sum(c["elapsed_seconds"] for c in cases) / len(cases)

print(f"Verification Passed: {passed_count} / {len(cases)} ({passed_count/len(cases)*100:.1f}%)")
print(f"Verification Failed / Warning: {failed_count} / {len(cases)} ({failed_count/len(cases)*100:.1f}%)")
print(f"Vivarix Executed: {vivarix_count} / {len(cases)} ({vivarix_count/len(cases)*100:.1f}%)")
print(f"Lumicell Executed: {lumicell_count} / {len(cases)} ({lumicell_count/len(cases)*100:.1f}%)")
print(f"Average Elapsed Time: {avg_elapsed:.2f}s")

print("=" * 100)
all_defects = []
defect_categories = Counter()
for c in cases:
    vr = c.get("verification_report")
    if vr and vr.get("defects"):
        for d in vr["defects"]:
            all_defects.append(d)
            defect_categories[d["category"]] += 1

print(f"Total Verification Defects across all cases: {len(all_defects)}")
for dcat, cnt in defect_categories.items():
    print(f"  Defect Category '{dcat}': {cnt}")

print("=" * 100)
for c in cases:
    vr = c.get("verification_report") or {}
    defs = vr.get("defects", [])
    print(f"{c['case_id']} | {c['category']:<32} | {c['elapsed_seconds']:>6.2f}s | Viv: {str(c['vivarix_executed']):<5} | Lumi: {str(c['lumicell_executed']):<5} | Pass: {str(c['verification_passed']):<5} | Def: {len(defs):>2} | {c['query'][:38]}")
