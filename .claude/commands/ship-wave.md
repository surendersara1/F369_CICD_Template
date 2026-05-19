---
description: Commit + push the current wave to both F369 repos atomically. Args: <wave_number> "<one-line summary>"
argument-hint: <wave_NN> "<short summary>"
---

User is shipping wave `$1` with summary `$2`.

Steps:

1. **Verify both repos have changes.** Run:
   - `git -C /e/F369_CICD_Template status --short`
   - `git -C /e/F369_LLM_TEMPLATES status --short`

   If either repo is clean and the other has changes, ask the user before proceeding (lockstep violation possible).

2. **List staged + unstaged changes** in both repos. Verify the change set matches a wave shape:
   - CICD repo: N new partials + README.md update
   - LLM repo: M new composite templates + Library.md update
   
   Flag any unrelated changes that snuck in (e.g., random file edits).

3. **Construct commit messages** per `.claude/rules/wave-commits.md`:
   - Read the new partials' titles to extract concise descriptions for the commit body
   - Same for new templates
   - Pull the OLD partial count from git diff of README.md; extract NEW count
   - Same for Library.md template count

4. **Show user the proposed commit messages** for both repos. Wait for approval.

5. **On approval, execute commits** using the HEREDOC pattern with `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>` trailer.

6. **Push both** with `2>&1 | tail -3` to capture push output:
   - `git -C /e/F369_CICD_Template push origin main 2>&1 | tail -3`
   - `git -C /e/F369_LLM_TEMPLATES push origin main 2>&1 | tail -3`

7. **Verify clean state**:
   - Both `git status` → clean
   - Both `git log @{u}..HEAD` → empty (no unpushed commits)

8. **Report**:
   ```
   Wave $1 shipped.
   CICD: <commit-sha-short> — <N partials>
   LLM:  <commit-sha-short> — <M templates>
   Both pushed to origin/main; working trees clean.
   ```
