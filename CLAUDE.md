# F369 CICD Template — Claude project instructions

This file is loaded automatically by Claude Code on every session in this repo. It encodes the project's structure, working patterns, and the discipline that keeps the library coherent.

## What this repo is

The **F369 partial library** — atomic SOPs (Standard Operating Procedures) that compose into AWS engagement playbooks. Three-tier system across two repos:

- **Partial** (this repo, `prompt_templates/partials/`) — atomic SOP for one AWS concern (e.g., `EKS_KARPENTER_AUTOSCALING.md`, `BEDROCK_KNOWLEDGE_BASES.md`)
- **Composite Template** (sister repo `E:/F369_LLM_TEMPLATES/`) — chains 5–15 partials into an engagement-grade playbook (e.g., `enterprise/09_landing_zone_baseline.md`)
- **Kit** (sister repo) — business-first wrapper around a composite template for client-facing case studies

Library size as of 2026-04-28: **143 partials × 101 composite templates** across 10 categories (data, mlops, cicd, iac, devops, finops, enterprise, edge, backend, migration).

## Sister repos & working dirs

| Path | Purpose |
|---|---|
| `E:\F369_CICD_Template` | This repo. Partials live in `prompt_templates/partials/`. |
| `E:\F369_LLM_TEMPLATES` | Composite templates + kits. Library overview in `Library.md`. |
| `E:\NBS_Research_America_regen` | Reference implementation kit (real client). |

The user's preference: when work spans both partial repos, commit + push to **both** atomically per wave.

## The Canonical-Copy Rule (READ — non-negotiable)

When authoring a new partial that uses a CDK primitive, AWS service, or IAM pattern already covered by an audited canonical partial, **open the canonical partial and copy the audited code verbatim**. Adapt only variable names + logical IDs. Never re-derive from memory.

Why: three audit rounds (R1, R2, R3) caught the same failure mode — re-deriving causes schema hallucinations (e.g., `filterable_metadata_keys` on `AWS::S3Vectors::Index` doesn't exist; the canonical `DATA_S3_VECTORS.md` documents the correct schema).

The full registry of canonical partials + which sections to copy from is in `prompt_templates/partials/README.md` § "Common 'what to copy' answers".

## Canonical structure for a new partial

Every new partial in `prompt_templates/partials/` follows this 13-section structure (see any recent partial for reference, e.g., `EKS_KARPENTER_AUTOSCALING.md` or `BEDROCK_KNOWLEDGE_BASES.md`):

1. Header (version, last-reviewed, status, applies-to)
2. Purpose (bulleted; what it codifies)
3. Decision tree (when this vs alternatives)
4. Variants (Monolith for POC + Multi-stack/Production)
5. CDK code blocks (Python; full, not snippets)
6. Common gotchas
7. Pytest worked example
8. 5 non-negotiables
9. References
10. Changelog table

Each partial averages 600–900 lines.

## Wave-based work pattern

Work is organized in numbered "Waves" (Wave 1 = exemplar partials; Wave 19 = current). A wave typically delivers:

- 3–9 new partials in `prompt_templates/partials/`
- 2–3 composite templates in the sister repo's category folder
- Updates to `prompt_templates/partials/README.md` (count + registry rows + "what to copy" table)
- Updates to sister repo's `Library.md` (count + category sections)
- Atomic commit per repo + push to both

Naming: branch == `main`. Commits use conventional `add:`, `update:`, `fix:` prefixes followed by an em-dash and the wave description.

## Commit message style (the user's preference)

**Use HEREDOC for multi-line commit messages.** The user's pattern:

```bash
git commit -m "$(cat <<'EOF'
add: N v2.0 partials — Wave NN <one-line summary>

<2-4 sentence rationale: what gap this closes, why it matters>

- PARTIAL_NAME_1 — short description
- PARTIAL_NAME_2 — short description
- PARTIAL_NAME_3 — short description

Registry updated (PREV -> NEW partials). Wave NN.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Always include the `Co-Authored-By` trailer.

## Auto Mode preference

The user runs Auto Mode. Concretely:
- Execute immediately; minimize confirmation prompts for routine work.
- Make reasonable assumptions; user will course-correct.
- Prefer action over planning for incremental work (new partials, registry updates, commits).
- **Still pause for confirmation** on: destructive git operations (force-push, hard reset), shared-system changes, secrets exfiltration, irreversible AWS resource changes.

## Output style preferences (learned from user feedback)

- **Terse responses + no trailing summaries.** The user reads the diff; don't recap what was just written.
- **Don't be shy about scope.** When user says "build them all", deliver the full set in one session.
- **Batch commits per wave**, not per partial.
- **Update the README registry + Library.md count line** in the same commit as the new artifacts.
- **Date stamps**: changelog tables use today's date in `YYYY-MM-DD` format. Today's date is in the system context.

## Non-obvious things to remember

- The user is **NorthBay Solutions** consulting; F369 = "369Forecast Elite Capital" engagement framework. Partials power 2–3 day POCs and weekend builds.
- Two repos must stay in lockstep (partials in one, composites in the other). Don't ship one without the corresponding other.
- The user often asks "what next" — the canonical answer pattern is: 3–4 ranked candidates with one recommended, await go-signal.
- The user prefers `Co-Authored-By: Claude Opus 4.7 (1M context)` exactly (not generic "Claude").
- GitHub auth: pre-configured via git credential manager; `gh` CLI is installed.

## Key files when starting a new wave

1. `prompt_templates/partials/README.md` § "Canonical Partials Registry" — what's already covered
2. Latest partial in same category as exemplar of structure
3. `E:\F369_LLM_TEMPLATES\Library.md` — overall template inventory
4. `E:\F369_CICD_Template\.claude\rules\` — modular conventions (this directory; consult before authoring)

## When in doubt

- New partial author: read `.claude/rules/partial-authoring.md`
- New composite template: read `.claude/rules/template-authoring.md`
- Commit/push across repos: read `.claude/rules/wave-commits.md`
