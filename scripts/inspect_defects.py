import json

cases = json.load(open("AUDIT_EVALUATION_CASES.json", encoding="utf-8"))
print("Non-numerical defects across evaluation cases:")
print("-" * 80)
for c in cases:
    vr = c.get("verification_report") or {}
    for d in vr.get("defects", []):
        if d.get("category") != "NUMERICAL":
            print(f"{c['case_id']} | {d.get('category')} | {d.get('severity')} | {d.get('description')}")
