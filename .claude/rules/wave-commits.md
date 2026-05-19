# Rule — Wave commits across both repos

Apply when: shipping a wave (new partials + new composite templates).

## Lockstep principle

The two repos must stay in sync per wave. Never commit only one side.

- **Partials** live in `E:/F369_CICD_Template/prompt_templates/partials/`
- **Composite templates** live in `E:/F369_LLM_TEMPLATES/<category>/`
- Both update their respective indexes (`README.md` for partials, `Library.md` for templates)

## Commit message template

Use HEREDOC for both commits. The user's pattern:

### Partials commit (CICD repo)

```bash
git -C /e/F369_CICD_Template add \
  prompt_templates/partials/PARTIAL_A.md \
  prompt_templates/partials/PARTIAL_B.md \
  prompt_templates/partials/PARTIAL_C.md \
  prompt_templates/partials/README.md \
&& git -C /e/F369_CICD_Template commit -m "$(cat <<'EOF'
add: N v2.0 partials — Wave NN <one-line summary>

<2-4 sentences: what gap this closes; why it matters now>

- PARTIAL_A — short description (one line)
- PARTIAL_B — short description
- PARTIAL_C — short description

Registry updated (PREV -> NEW partials). Wave NN.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Composite templates commit (LLM_TEMPLATES repo)

```bash
git -C /e/F369_LLM_TEMPLATES add \
  <category>/<NN>_<name>.md \
  Library.md \
&& git -C /e/F369_LLM_TEMPLATES commit -m "$(cat <<'EOF'
add: M v2.0 composite templates — Wave NN <category> kits

<one-paragraph context>

- <category>/NN_template_a (timeline) — what it ships
- <category>/NN_template_b (timeline) — what it ships

Library.md: PREV -> NEW templates; <category> X -> Y.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Push both

```bash
git -C /e/F369_CICD_Template push origin main 2>&1 | tail -3
git -C /e/F369_LLM_TEMPLATES push origin main 2>&1 | tail -3
```

## Always include

- `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>` (exact spelling, not generic "Claude")
- Wave number in the message body
- Old → new count for registry/library
- "Wave NN" trailer line for grep-ability

## Verify before celebrating

After push, verify both clean + up-to-date with origin:

```bash
git -C /e/F369_CICD_Template status
git -C /e/F369_LLM_TEMPLATES status
```

Both should show `nothing to commit, working tree clean` + `Your branch is up to date with 'origin/main'`.

## Anti-patterns

- Committing partials without updating `README.md` count + registry → drift
- Committing composite templates without updating `Library.md` → drift
- Wave-naming the partials commit but generic-naming the templates commit → grep-broken
- `git add -A` or `git add .` → risk of leaking `.claude/settings.local.json` or scratch files; always add specific files

## Atomicity

- One wave = one commit per repo (not one commit per partial).
- Push both within minutes of each other; never leave one repo ahead of the other for hours.
