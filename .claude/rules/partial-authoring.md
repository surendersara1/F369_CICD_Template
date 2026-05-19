# Rule — Authoring a new partial

Apply when: creating a new file in `prompt_templates/partials/` or modifying an existing partial's structure.

## Hard rules

1. **Canonical-Copy Rule first.** Before writing any CDK code block, check `prompt_templates/partials/README.md` §Registry for canonical partials covering the same primitive. Open them. Copy the §3 / §4 code block. Adapt names only. Don't re-derive from memory — that's how schema hallucinations get re-introduced.

2. **Use the 13-section canonical structure** (see any recent partial, e.g., `EKS_KARPENTER_AUTOSCALING.md`, `BEDROCK_KNOWLEDGE_BASES.md`):
   1. Header — version, last-reviewed (today's date), status, applies-to
   2. § 1 Purpose — bulleted; what this codifies
   3. § 2 Decision tree — when this vs alternatives + variant selector
   4. § 3 Monolith Variant — POC-grade single-stack
   5. § 4 (optional) Production Variant — multi-stack
   6. § 5+ Per-feature deep dives (cost, alternatives, advanced)
   7. § N Common gotchas — proactive warnings
   8. § N+1 Pytest worked example — boto3 assertions
   9. § N+2 Five non-negotiables — bulleted
   10. § N+3 References — official AWS docs
   11. § N+4 Changelog table — version + date + change

3. **CDK code is full, not snippets.** Show real `aws_cdk.aws_*` imports, real `Construct` classes, real `__init__` signatures. Reader should be able to lift the code into their CDK app with name changes only.

4. **Pytest examples use boto3.** Don't use moto for production code paths; show real assertion against deployed resources.

5. **Each partial averages 600–900 lines.** Shorter = under-specified; longer = should be split into multiple partials.

## After authoring

1. Update `prompt_templates/partials/README.md`:
   - Count line at top (`**Count:** N v2.0 partials …`)
   - Add row in the appropriate "### Category" registry table
   - Add "what to copy" entry in the §"Common what to copy answers" table

2. Don't forget the changelog row inside the partial itself.

## Anti-patterns (caught in audits)

- Re-deriving CDK from memory when a canonical partial exists → schema hallucinations
- Inline IAM policies with `Resource: "*"` for sensitive actions → audit failures
- Skipping pytest section because "trivial" → leaves no validation contract
- Skipping Common Gotchas because "code is self-evident" → leaves users to step on the same rake

## Reference

Full canonical structure + audit history in `prompt_templates/partials/README.md` and `prompt_templates/partials/_prompts/build_remaining_partials_v2.md` (the build prompt).
