# reverie-workflows

Reusable GitHub Actions workflows for Reverie Development repositories.

## Dependabot auto-merge

`.github/workflows/dependabot-auto-merge.yml` handles Dependabot pull requests:

- A patch or minor update gets native auto-merge. GitHub merges it when the base branch's required status checks pass.
- A major update gets the `dependencies: major` label and one comment. A maintainer merges it.
- A patch or minor GitHub Actions update gets the `dependencies: actions` label. A maintainer merges it, because `GITHUB_TOKEN` cannot hold the `workflows` permission. A major GitHub Actions update gets `dependencies: major`.
- A weekly run closes a grouped Dependabot PR that a newer one supersedes.

Preconditions: the repository allows auto-merge, and the base branch's required status checks are defined in a repository ruleset. The workflow reads rulesets only, not classic branch protection, and it refuses to arm auto-merge, with an error, when either precondition fails.

### Caller

```yaml
name: Dependabot auto-merge

on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
  schedule:
    - cron: "0 7 * * 1"

permissions: {}

jobs:
  dependabot:
    if: github.event_name == 'schedule' || github.event.pull_request.user.login == 'dependabot[bot]'
    permissions:
      contents: write
      pull-requests: write
    uses: Reverie-Development-Inc/reverie-workflows/.github/workflows/dependabot-auto-merge.yml@<commit-sha>
    with:
      auto-merge: true
```

Pin `uses:` to a full commit SHA.

### Inputs

| Input | Default | Meaning |
| --- | --- | --- |
| `auto-merge` | `true` | Arm auto-merge for patch and minor updates. Set `false` where a merge deploys or publishes: majors and GitHub Actions updates are still labelled, and other patch and minor updates are left for a maintainer. |
| `merge-method` | `squash` | `squash`, `merge` or `rebase`. |
| `major-label` | `dependencies: major` | Label for major updates. |
| `actions-label` | `dependencies: actions` | Label for patch and minor GitHub Actions updates. |

## Tests

`python tests/test_dependabot_auto_merge.py` reads the arming and weekly sweep step scripts out of the workflow file and runs them against a stub `gh`. CI runs it on every change.
