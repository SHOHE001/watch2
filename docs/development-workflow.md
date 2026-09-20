# Development workflow

Repository: SHOHE001/watch2 (public), default branch `main`. Reuse Development HQ for Issue status and Agent=Codex. Work through an Issue and branch, create a PR, verify all CI on its current head and unresolved reviews, merge normally, then close the Issue and set Project Done.

The repository initially had no CI or local development instructions. Python 3.11 and 3.12 CI now run the synthetic test suite and build distribution packages. Never use live Discord/tmux or personal transcripts as regression fixtures.

On 2026-09-21 the branch-protection API returned 404 and the rulesets list was empty. No protection settings were changed; PR and successful CI remain the required working process.

Input delivery must preserve each prompt's literal-text/Enter pair. Commands aimed at the same configured tmux target share a lock, including different mapped channels. Different targets remain independent.
