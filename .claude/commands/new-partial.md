---
description: Scaffold a new partial in prompt_templates/partials/ following the canonical 13-section structure. Args: <PARTIAL_NAME> [category]
argument-hint: <PARTIAL_NAME_UPPERCASE_UNDERSCORES> [category]
---

User wants to create a new partial named `$1` (category optional: `$2`).

Steps:

1. **Check for canonical overlap.** Read `prompt_templates/partials/README.md` § "Common 'what to copy' answers" and § "Canonical Partials Registry". Identify any canonical partial whose patterns this new partial should copy (CDK constructs, IAM patterns, service APIs).

2. **Read the closest canonical partial in full** to internalize its CDK style + section flow. If category is given, read 1–2 recent partials from that category as additional reference.

3. **Confirm scope with the user before writing.** Ask: "Based on $1, I'll cover: <bulleted scope>. Composes from: <canonical partials>. Want me to proceed?"

4. **On confirmation, author the partial** in `prompt_templates/partials/$1.md` following the canonical 13-section structure (see `.claude/rules/partial-authoring.md`):
   - Header with version 2.0 + today's date + status Active
   - § 1 Purpose
   - § 2 Decision tree
   - § 3 Monolith Variant with full CDK
   - § 4 Production Variant (if substantively different)
   - § 5+ Per-feature deep dives as needed
   - Common gotchas
   - Pytest worked example (boto3-based)
   - Five non-negotiables
   - References to AWS docs
   - Changelog table

5. **Update `prompt_templates/partials/README.md`**:
   - Bump partial count line at top
   - Add row in the appropriate `### Category` registry table
   - Add "what to copy" entry in §"Common what to copy answers"

6. **Don't commit yet.** Show a summary of changes for user review:
   - File created: <path>
   - Lines: <count>
   - Updates to README.md: <list>
   - Suggested commit-when-ready command (HEREDOC pattern from `.claude/rules/wave-commits.md`)
