# Development agreements

- Use Issue → work branch → PR → successful CI → normal merge; never directly push main or bypass checks. Preserve unrelated changes.
- Run `python -m pytest -q` and package build checks. Tests must use fake Discord clients, injected tmux runners and temporary JSONL, without real sessions or message delivery.
- Never commit tokens, personal config, session transcripts or environment files.
- Record durable objectives in Issues, changes/checks in PRs and design in docs; synchronize the existing Development HQ project. See `docs/development-workflow.md`.
- After three evidence-backed failed attempts at the same issue, record the blocker and continue independent work.
