---
description: Show current state of both F369 repos — pending changes, latest waves, partial/template counts.
---

Run these commands and produce a single concise status report:

1. `git -C /e/F369_CICD_Template status --short`
2. `git -C /e/F369_LLM_TEMPLATES status --short`
3. `git -C /e/F369_CICD_Template log --oneline -5`
4. `git -C /e/F369_LLM_TEMPLATES log --oneline -5`
5. Read line 1-5 of `prompt_templates/partials/README.md` and extract the partial count.
6. Read line 113-115 of `E:/F369_LLM_TEMPLATES/Library.md` to extract the template total.

Then output:

```
F369 status

CICD repo:    <clean|N pending>
              Latest: <commit message>
              Partials: <count>

LLM repo:     <clean|N pending>
              Latest: <commit message>
              Templates: <count>

Lockstep:     <both clean / drift / N waves apart>
```

Keep the output to ~10 lines. No prose around it.
