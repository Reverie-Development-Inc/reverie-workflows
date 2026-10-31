# reverie-workflows

Reusable GitHub Actions workflows for Reverie Development repositories.

## Dependabot auto-merge

`.github/workflows/dependabot-auto-merge.yml` handles Dependabot pull requests:

- A patch or minor update gets native auto-merge. GitHub merges it when the base branch's required status checks pass. A GitHub Actions update gets auto-merge only when `arm-github-actions` is `true` as well.
- Every GitHub Actions update that fetch-metadata can read also gets the `dependencies: actions` label as a marker, whatever its update type. A PR that fetch-metadata rejects gets only the unknown label.
- A major update gets the `dependencies: major` label and one comment. A maintainer merges it.
- These updates get the hold label, `dependencies: ready` by default, instead of auto-merge, and a maintainer merges them:
  - A patch or minor update when `auto-merge` is `false`.
  - A patch or minor GitHub Actions update when `arm-github-actions` is `false`.
  - Every update with a known type in a group that `hold-groups` lists.
  - Every update with a known type whose commits the API cannot list in full (none, or 250 or more), or whose head moved after the run started. The run for the new head decides again.
- An update without a known type for every dependency gets the unknown label, `dependencies: review` by default, one comment and a warning. It is never armed, and a person reviews it. Some bumps from one commit SHA to another have no type. A PR that fetch-metadata rejects, for example because its oldest commit is not a verified Dependabot commit, gets the same label and one comment.
- The workflow removes a hold or unknown label that no longer applies. For example, it removes the hold label after you turn on `arm-github-actions`, and the unknown label after the update type becomes known.
- A PR with a commit that is not a verified Dependabot commit, such as a fix that a person pushed on top or a Dependabot commit that a maintainer amended and signed, is never armed. It gets the hold label, unless its update type is unknown. When a maintainer amends the oldest commit without signing it, fetch-metadata rejects the PR, so it gets the unknown label instead. fetch-metadata checks only the oldest commit of a PR, so the workflow checks the author, the committer and the signature of every commit. Dependabot's commits have the committer `web-flow`.
- The workflow arms auto-merge only for the newest commit that it checked, and only when that commit is the head that the triggering event names. GitHub refuses to arm a head that changed after the check. But a later push by someone with write access does not turn off auto-merge. Auto-merge stays on until the workflow run for that push turns it off, and if the required checks pass first, the new head can merge. A base-branch change by someone with write access, and a push that starts no workflow run, such as a push with `GITHUB_TOKEN` or with `[skip ci]` in its head commit message, keep auto-merge on with no run to turn it off. Turn off auto-merge before you push to an armed Dependabot PR or change its base branch.
- When a run does not arm a PR, the workflow turns off auto-merge that `GITHUB_TOKEN` enabled. This includes a run in which a step fails, for example when GitHub refuses to arm a head that changed after the check, or when fetch-metadata rejects the PR. GitHub reports every workflow that uses `GITHUB_TOKEN` as the same bot. So the workflow also turns off auto-merge that another workflow in the repository enabled with `GITHUB_TOKEN`. Auto-merge that a person enabled stays on, and the workflow does not arm over it.
- A weekly run closes a grouped Dependabot PR that a newer one supersedes, with a comment that names the newer PR. The PR title identifies a grouped PR, its group and its directories. The run compares only PRs with the same base branch, the same branch name before the hash, and the same group and directories. It keeps the older PR open, and logs a notice with the reason, unless both of these are true:
  - The newer PR changes every file that the older PR changes. The API lists at most 3000 files of a PR, so the older PR stays open when either list reaches 3000.
  - Every commit on the older PR is a verified Dependabot commit, by the same check that the workflow makes before it arms a PR. So the run does not close a PR that has a fix from a person, or delete its branch. The API lists at most 250 commits of a PR, so the older PR stays open when its list reaches 250.

The workflow creates only the labels that the repository does not have. It does not change the color or description of an existing label. As on GitHub, label names match without regard to case.

Runs for the same PR do not overlap. GitHub keeps one pending run for each PR. A newer run cancels an older pending run, and the newest run executes.

Preconditions: the repository allows auto-merge, and the base branch's required status checks are defined in a repository ruleset. The workflow reads rulesets only, not classic branch protection, and it refuses to arm auto-merge, with an error, when either precondition fails. A failed arm leaves the PR with no hold label and no auto-merge that `GITHUB_TOKEN` enabled, and the run fails.

A red required check keeps the PR open, because auto-merge waits for green. The workflow never merges on its own evidence.

A person must add a gate label, which is any label that a required check needs. A label that `GITHUB_TOKEN` adds starts no workflow run, so the gate would never run. Auto-merge stays armed and merges once that run is green.

### GitHub Actions updates

A GitHub Actions update changes files under `.github/workflows/` and brings in third-party action code. The workflow arms it only when `auto-merge` and `arm-github-actions` are both `true`. A patch or minor GitHub Actions update that is not armed because `auto-merge` or `arm-github-actions` is off gets the hold label. If GitHub refuses an armed merge, the PR stays open with its marker label.

fetch-metadata reads the ecosystem from the branch name, and the Dependabot `pull-request-branch-name` options can change or drop that part of the name. So the workflow treats a PR as a GitHub Actions update when the ecosystem is `github_actions`, `github-actions` or `github` in any letter case. It also does so when the PR changes a file under `.github/workflows/`, or a file named `action.yml` or `action.yaml`. A PR whose file list reaches the API's cap of 3000 files counts too, because the cap can hide such a file.

Set `arm-github-actions: true` only when every `github-actions` entry in `.github/dependabot.yml` has a Dependabot `cooldown`. An armed update merges third-party action code without a person reading it first. A cooldown makes Dependabot wait that number of days after a release before it proposes the release. This repository uses 7 days:

```yaml
  - package-ecosystem: github-actions
    directory: /
    schedule:
      interval: weekly
    cooldown:
      default-days: 7
```

Dependabot applies `cooldown` to version updates only, not to security updates. With `arm-github-actions: true`, the workflow arms a security update for an Action as soon as it opens. The workflow cannot tell a security update from a version update. fetch-metadata reports that only with its `alert-lookup` option, which needs a token that can read Dependabot alerts, and this workflow uses `GITHUB_TOKEN` only.

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

### Hold a group

A date-style version change, such as 2026.7.1 to 2026.10.5, counts as a minor update. A group of such dependencies auto-merges unless you list it in `hold-groups`:

```yaml
    with:
      auto-merge: true
      hold-groups: calendar-versioned, build-tools
```

Use the group names from `.github/dependabot.yml`. A name matches only a group with that full name, and the subgroups that `group-by: dependency-name` makes from it, which are named `<name>/<dependency>`. A PR of a listed group is never armed. It gets the hold label when its update type is known, and the unknown label when it is not.

`hold-groups` matches Dependabot groups only. To hold an ungrouped dependency, put it in a group in `.github/dependabot.yml`.

A Dependabot group covers version updates only, unless it sets `applies-to: security-updates`. To hold the security updates of a dependency too, define a security-updates group for it and list that group in `hold-groups` as well.

### Inputs

| Input | Default | Meaning |
| --- | --- | --- |
| `auto-merge` | `true` | Arm auto-merge for patch and minor updates. Set `false` where a merge deploys or publishes. Then a patch or minor update gets the hold label instead, and a maintainer merges it. Major, unknown and GitHub Actions updates get their labels in both cases. |
| `arm-github-actions` | `false` | Also arm patch and minor GitHub Actions updates when `auto-merge` is `true`. Set `true` only when every `github-actions` entry in `.github/dependabot.yml` has a Dependabot `cooldown`. |
| `merge-method` | `squash` | `squash`, `merge` or `rebase`. |
| `major-label` | `dependencies: major` | Label for major updates. |
| `actions-label` | `dependencies: actions` | Marker label for every GitHub Actions update that fetch-metadata can read. |
| `hold-label` | `dependencies: ready` | The hold label. It marks a patch or minor update that is not armed because of `auto-merge` or `arm-github-actions`. It also marks an update with a known type that is in a `hold-groups` group, has a commit that Dependabot did not make, has commits that the API cannot list in full, or has a head that moved after the run started. |
| `unknown-label` | `dependencies: review` | The unknown label. It marks an update without a known type for every dependency. |
| `hold-groups` | empty | Comma-separated Dependabot group names. The workflow ignores whitespace around a name. A PR of a listed group or of its `group-by` subgroups is never armed. It gets the hold label when its update type is known, and the unknown label when it is not. It matches groups only, so put an ungrouped dependency in a group to hold it. A group covers version updates only, unless it sets `applies-to: security-updates`, so also list a security-updates group to hold security updates. |

## Tests

`python tests/test_dependabot_auto_merge.py` reads the workflow file and runs it against a stub `gh`. It runs all steps of the triage job in order for each case, and it runs the weekly sweep against a fixture of open PRs with their files and commits. CI runs it on every change.
