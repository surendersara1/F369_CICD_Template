# Rule — Authoring a composite template (sister repo)

Apply when: creating a new file in `E:/F369_LLM_TEMPLATES/<category>/`.

## Where templates live

| Category | Purpose |
|---|---|
| `mlops/` | SageMaker AI + Bedrock GenAI (Q Business, KBs, Agents) |
| `data/` | Lakehouse, streaming, DQ, mesh |
| `enterprise/` | Multi-account governance, security ops, DR, mesh |
| `backend/` | Serverless API + ECS + CloudFront edge |
| `migration/` | MGN + DMS + DataSync + Refactor Spaces |
| `devops/` | CI/CD, observability, Q Developer rollout |
| `cicd/` | Build pipelines |
| `iac/` | CDK + Terraform |
| `finops/` | Cost optimization |
| `edge/` | IoT + Outposts + edge devices |

## Canonical structure

Every composite template has these sections (see `mlops/34_q_business_enterprise_assistant.md` or `enterprise/09_landing_zone_baseline.md` for reference):

1. **Header** — Template Version + F369 wave + Composes list (which partials)
2. **# Title** — `Template NN — <Engagement Name>`
3. **## Purpose** — 1–2 paragraphs: what this engagement delivers + when used
4. **## Role Definition** — "You are an expert AWS X with deep expertise in: …"
5. **## Context and Inputs** — code-block list of `[REQUIRED]` + `[optional]` parameters
6. **## Partial Library (Claude MUST load)** — table of partials chained, with rationale
7. **## Architecture** — ASCII art diagram of the deployed stack
8. **## Day-by-day execution** — concrete daily breakdown (POC = 2–5 days; Program = 4–12 weeks)
9. **## Validation criteria** — checkbox list of must-pass tests
10. **## Common gotchas** — what tends to go wrong
11. **## Output artifacts** — numbered list of what gets generated (CDK stacks, runbooks, dashboards, tests)
12. **## Changelog** — table

## Hard rules

1. **Composes list at top** (HTML comment) lists every partial chained — Claude uses this to pre-load context.
2. **Engagement timeline must be realistic** — match real-world POC velocity (2–5 days for monolith POC, 4–6 weeks for production program).
3. **Architecture diagram is ASCII art**, not external image links.
4. **Day-by-day is concrete**, not vague — list specific deliverables per day.
5. **Validation criteria is testable** — every checkbox should be verifiable via boto3 / kubectl / curl.

## After authoring

1. Update `E:/F369_LLM_TEMPLATES/Library.md`:
   - Total count line at bottom
   - Category section: count + entry for new template
2. If new category, add the category section to the tree at top of `Library.md`.

## Anti-patterns

- Composing partials that don't exist → broken references; always check registry first
- Vague day-by-day ("Day 1: Set up infrastructure") → reader gains nothing
- Skipping pytest validation criteria → no exit gate for POC
- Inflating timeline ("8-week POC") → user signals over-engineered scope
