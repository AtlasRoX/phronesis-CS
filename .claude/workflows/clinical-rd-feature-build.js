export const meta = {
  name: 'clinical-rd-feature-build',
  description: 'Implement 8 Clinical R&D features: foundation hooks, 8 parallel feature builds with per-feature review, integration + live smoke test, final review and fixes',
  phases: [
    { title: 'Foundation', detail: 'shared hooks, stubs, slots, dependencies (serial)' },
    { title: 'Build', detail: 'one implementer per feature, disjoint file ownership' },
    { title: 'Feature review', detail: 'per-feature adversarial review + fix inside owned files' },
    { title: 'Integration', detail: 'integration requests, full tests, tsc, live smoke on :8011' },
    { title: 'Final review', detail: 'backend + frontend reviewers, then a fixer' },
  ],
}

const S = 'C:\\Users\\ghotc\\AppData\\Local\\Temp\\claude\\A--phroneeee\\778ff7cb-efc0-446c-ba43-af7766300132\\scratchpad'
const PLAN = `${S}\\plan.json`
const RESEARCH = `${S}\\research.json`
const SNAP = `${S}\\snapshot_post_foundation`
const BACKUP = `${S}\\backup_pre_features`

const COMMON = `
Project: Phronesis Clinical R&D assistant at A:\\phroneeee (Windows 11). Python venv: A:\\phroneeee\\.venv\\Scripts\\python.exe (run pytest from A:\\phroneeee). Frontend: A:\\phroneeee\\web (Next.js 16 / React 19; read web/AGENTS.md — this Next.js version has breaking changes, docs in web/node_modules/next/dist/docs/). Type-check with: cd web && npx tsc --noEmit -p .
The full implementation plan is the JSON file ${PLAN} (keys: plan_markdown, foundation_spec, packages[], integration_spec). Live API research with real field paths is in ${RESEARCH}. Read the parts relevant to you in full before coding.
Hard rules:
- NEVER fabricate biomedical data. Every number/identifier shown to users must come from a real tool/API output. Tests use trimmed REAL responses saved as fixtures; tests must run offline (no network).
- Ports 8000 and 3000 belong to the user's running servers: never start, stop, or send traffic to them. Use port 8011 (backend) / 3011 (frontend) only if your stage explicitly allows servers.
- Do not git commit. Do not delete the user's data (.phronesis_memory, data/).
- Match surrounding code style; keep comments sparse and meaningful. Python with -I when executing scripts that read downloaded files.
- Match existing Phronesis agent branding (Synapta, Vivarix, Lumicell, Lithoscribe, WebSurfer, BioDiscovery, Verifier).
`

const REPORT_SCHEMA = {
  type: 'object',
  required: ['status', 'summary', 'files_changed', 'tests', 'integration_requests', 'known_gaps'],
  properties: {
    status: { type: 'string', enum: ['done', 'partial', 'failed'] },
    summary: { type: 'string' },
    files_changed: { type: 'array', items: { type: 'string' } },
    tests: { type: 'string', description: 'exact commands run and pass/fail counts' },
    integration_requests: { type: 'array', items: { type: 'string' }, description: 'shared-file changes still needed, precise' },
    known_gaps: { type: 'array', items: { type: 'string' } },
  },
}

// ---------------------------------------------------------------- Foundation
phase('Foundation')
const foundation = await agent(`${COMMON}
YOUR ROLE: FOUNDATION implementer (you run alone, before 8 parallel feature agents). Implement plan.foundation_spec completely and exactly: dependencies (add to pyproject.toml and install into the existing venv with uv, e.g. "uv add python-multipart python-docx fpdf2 markdown-it-py" from A:\\phroneeee, or the spec's list), all hook modules, plugin/extension registries, stubs with exact signatures, shared-file hook edits (orchestrator, prompts, verifier, memory, registry, vivarix/lithoscribe signatures, discovery, main.py router mounting), frontend types/slot components/MessageArtifacts/MarkdownRenderer citations prop/ThinkingProcess plan chips/page.tsx wiring, tests/fakes.py.
Behavior must be IDENTICAL to today when no feature plugin is active: the existing 63 pytest tests must still pass (run the full suite: .venv\\Scripts\\python.exe -m pytest tests -q -p no:cacheprovider), and npx tsc --noEmit must pass. Read the current files first — some (page.tsx, globals.css, orchestrator) were edited recently and differ from older descriptions; adapt the spec to the real code rather than overwriting newer work.
Feature agents will NOT be allowed to edit shared files afterwards, so make hooks generic and complete; document every hook in a new file docs/plans/foundation-hooks.md (signatures + how a feature registers).
When finished and green, snapshot the tree for later review diffs: robocopy A:\\phroneeee "${SNAP}" /E /XD node_modules .next .venv __pycache__ .git .pytest_cache /XF *.zip *.h5ad /NFL /NDL /NJH /NJS /NP  (robocopy exit codes < 8 are success).`,
  { label: 'foundation', phase: 'Foundation', effort: 'high', schema: REPORT_SCHEMA })

if (!foundation || foundation.status === 'failed') {
  log('Foundation failed; stopping before feature work.')
  return { foundation }
}
log(`Foundation ${foundation.status}: ${foundation.summary.slice(0, 200)}`)

// ---------------------------------------------------------------- Build + per-feature review
const KEYS = ['trials', 'drugs', 'entities', 'citations', 'grading', 'variants', 'uploads', 'export']

const features = await pipeline(
  KEYS,
  (key) => agent(`${COMMON}
YOUR ROLE: FEATURE implementer for work package "${key}". 7 other agents are implementing other packages IN THE SAME WORKING TREE at the same time.
Read: plan.packages[key="${key}"] (your owned_files, instructions, acceptance), plan.plan_markdown (rules for feature agents), docs/plans/foundation-hooks.md (the hooks the foundation built), and research.json entry for your feature. Foundation report: ${JSON.stringify(foundation).slice(0, 4000)}
Rules: edit ONLY your owned files (creating them as needed). Never edit shared/foundation files or another package's files — if you need a shared change, put the exact change under integration_requests. Do not install packages. Do not start servers (use FastAPI TestClient / fakes). Capture trimmed real API responses into your fixtures dir using small live requests, then make tests offline.
Implement the package fully to its acceptance criteria, including the frontend pieces you own. Verify: run your own test file(s); run the full pytest suite and report results (failures in other packages' in-progress files are not yours — say which); run npx tsc --noEmit and fix errors in YOUR files.`,
    { label: `build:${key}`, phase: 'Build', schema: REPORT_SCHEMA }),
  (built, key) => agent(`${COMMON}
YOUR ROLE: adversarial REVIEWER + fixer for work package "${key}", which another agent just implemented (its report: ${JSON.stringify(built).slice(0, 6000)}). Other packages may still be in progress in the same tree.
Read plan.packages[key="${key}"] and docs/plans/foundation-hooks.md. Then review hard, assuming bugs exist:
1. Ownership: diff the package's files and the shared files against the post-foundation snapshot ${SNAP} (e.g. with git diff --no-index or fc). Any edit by this package outside its owned_files is a defect: revert it there and convert it to an integration request.
2. Scientific integrity: could any path display a number/identifier not present in a real API response? Are API field paths correct per research.json (spot-check 1-2 live requests)? Are failures/empty results reported honestly as errors/limitations rather than silently? Are rate limits/timeouts handled?
3. Correctness: run its tests; read the code for edge cases (empty results, API errors, None fields, unicode, Windows paths); confirm the verifier recognizes the new identifier types; confirm the feature degrades gracefully when its tools are not registered (offline registry).
4. Frontend: tsc clean for this package's files; components handle missing data; consistent with existing CSS variables; accessible (buttons labelled, keyboard reachable).
Fix every confirmed defect INSIDE the package's owned files, re-run tests, and report.`,
    { label: `review:${key}`, phase: 'Feature review', schema: REPORT_SCHEMA })
    .then((rev) => ({ key, built, review: rev })),
)

const feats = features.filter(Boolean)
const lost = KEYS.filter((k) => !feats.find((f) => f.key === k))
if (lost.length) log(`Feature pipelines that produced nothing: ${lost.join(', ')}`)
const requests = feats.flatMap((f) => [...((f.built && f.built.integration_requests) || []), ...((f.review && f.review.integration_requests) || [])].map((r) => `[${f.key}] ${r}`))
log(`${feats.length}/8 features reported; ${requests.length} integration requests`)

// ---------------------------------------------------------------- Integration
phase('Integration')
const integration = await agent(`${COMMON}
YOUR ROLE: INTEGRATOR. All 8 feature packages are finished. You may now edit shared files.
Do, in order:
1. Implement plan.integration_spec.
2. Apply these integration requests from feature agents (judge each; apply the correct ones, explain any rejected):
${requests.join('\n') || '(none)'}
3. Feature status summaries: ${JSON.stringify(feats.map((f) => ({ key: f.key, build: f.built && { status: f.built.status, gaps: f.built.known_gaps }, review: f.review && { status: f.review.status, summary: f.review.summary, gaps: f.review.known_gaps } }))).slice(0, 12000)}
4. Run the FULL pytest suite and make it green; run cd web && npx tsc --noEmit and make it clean; run any web node tests the packages added.
5. Live smoke test: start the backend on port 8011 with PHRONESIS_AUTH_MODE=disabled (background), and stream real queries through POST http://127.0.0.1:8011/api/chat/stream that exercise every feature: e.g. "What clinical trial response rates have been reported for sotorasib and adagrasib in KRAS G12C non-small cell lung cancer, and what are their main adverse events?", "What is the clinical significance and frequency of KRAS G12D in pancreatic cancer, and what is SHP2's role?", plus an upload of test_atlas.h5ad via the datasets router then a query referencing it, plus DOCX and PDF export of a result and a re-run. Confirm each feature's steps, evidence, citations map, and artifacts appear and contain only real data; confirm verification still works. Fix what breaks. Stop the 8011 server when done. NEVER touch ports 8000/3000.
Report precisely what was verified live versus only in tests.`,
  { label: 'integrate', phase: 'Integration', effort: 'high', schema: REPORT_SCHEMA })

// ---------------------------------------------------------------- Final review
phase('Final review')
const FINDINGS = {
  type: 'object', required: ['findings'],
  properties: { findings: { type: 'array', items: { type: 'object', required: ['severity', 'file', 'problem', 'fix'], properties: {
    severity: { type: 'string', enum: ['critical', 'major', 'minor'] }, file: { type: 'string' }, problem: { type: 'string' }, fix: { type: 'string' } } } } },
}
const lenses = [
  { key: 'backend', prompt: 'Review the BACKEND changes since the pre-feature backup (' + BACKUP + ', diff with git diff --no-index): correctness bugs, fabricated or unverifiable data paths, verifier gaps for new identifier types, upload security (path traversal, per-user isolation, size limits, unsafe parsing), export injection/HTML issues, rate-limit/timeouts, error honesty, and test quality (are tests offline and meaningful?). Run the full pytest suite.' },
  { key: 'frontend', prompt: 'Review the FRONTEND changes since the pre-feature backup (' + BACKUP + '\\web\\src vs A:\\phroneeee\\web\\src): runtime bugs, missing-data crashes, React hook misuse, localStorage size growth, accessibility, dark/light theming via existing CSS variables, layout at 390px width, consistency with the existing thinking/answer UI. Run npx tsc --noEmit. You may start the frontend on port 3011 only if you also start a backend on 8011, and drive it headlessly with playwright-core from ' + S + '\\pw (chromium at %LOCALAPPDATA%\\ms-playwright\\chromium-1243\\chrome-win64\\chrome.exe); stop both afterwards.' },
]
const reviews = await parallel(lenses.map((l) => () =>
  agent(`${COMMON}\nYOUR ROLE: final ${l.key} reviewer (read-only: do not edit files). ${l.prompt}\nReport only confirmed problems with concrete evidence; no style nits.`,
    { label: `final-review:${l.key}`, phase: 'Final review', schema: FINDINGS })))
const findings = reviews.filter(Boolean).flatMap((r) => r.findings).filter((f) => f.severity !== 'minor' || true)
log(`Final review: ${findings.length} findings (${findings.filter((f) => f.severity === 'critical').length} critical, ${findings.filter((f) => f.severity === 'major').length} major)`)

let fixes = null
if (findings.length) {
  fixes = await agent(`${COMMON}\nYOUR ROLE: final fixer. Verify each finding below against the code; fix every one that is real (critical and major first, minor if cheap and safe); reject false ones with a reason. Keep the full pytest suite green and tsc clean. Do not start servers on 8000/3000.\nFINDINGS:\n${JSON.stringify(findings, null, 1)}`,
    { label: 'final-fix', phase: 'Final review', effort: 'high', schema: REPORT_SCHEMA })
}

return { foundation, features: feats, lost, integration, findings, fixes }
