# Private GitHub Actions runner recovery

**Status: 2026-11-12.** Hosted-runner allocation for the private repository is blocked before the first workflow step. The evidence strongly localizes the fault to a private-repository/account usage, plan, billing, or policy gate. The exact sub-cause is not exposed by the connected GitHub API and is not claimed as confirmed.

## Diagnostic evidence

- F-49 PR #4 is OPEN / DRAFT / UNMERGED and targets `feature/multi-stage-confirmation-engine`, not `main`. Before this recovery change, its code HEAD was `16a95d27b61b61d78e56e00ac50f12282f4c5e32`.
- Private [offline-tests run #970](https://github.com/Iman-Rashidi/crypto-super-radar-Visibility-Iman-Rashidi/actions/runs/43297902641) on that HEAD created `test` and `streamlit-smoke` jobs that failed in about four seconds. Job metadata showed `runner_id=0`, an empty runner name, no steps and no logs.
- Private [offline-tests run #971](https://github.com/Iman-Rashidi/crypto-super-radar-Visibility-Iman-Rashidi/actions/runs/43298036464) repeated the same pre-step failure on the same HEAD. The connector returned `steps=null` and `logs_url=null` for both jobs.
- In the latest 50 workflow runs visible in this repository, all 50 concluded FAILURE. The affected workflows include `offline-tests`, `runner-diagnostic`, and `live-provider-acceptance`; the pattern predates the F-49 branch.
- The actual `.github/workflows/offline-tests.yml` on the PR base uses standard `ubuntu-latest` runners and Python `3.14`. Its pull-request branch filter includes `feature/multi-stage-confirmation-engine`, so PR #4 matches it.
- `.github/workflows/runner-diagnostic.yml` uses `ubuntu-24.04` and starts with a simple shell step that prints runner metadata. That diagnostic has also failed before the first step. This makes changing `runs-on`, Python, or test code an unsupported fix.
- Positive controls in the same account: public [runner allocation run 43267300783](https://github.com/Iman-Rashidi/skills-hello-github-actions/actions/runs/43267300783) succeeded and had real runner steps. Public [Python 3.14 run 43267582063](https://github.com/Iman-Rashidi/skills-hello-github-actions/actions/runs/43267582063) successfully checked out code, installed Python 3.14, verified pip and installed dependencies. The public repository is `skills-hello-github-actions`.

**Assessment — high confidence:** the failure is specific to Actions on the private repository/account rather than a malformed workflow, unsupported Python version, or general hosted-runner outage.

**Still unconfirmed:** whether the specific block is exhausted included compute, a budget configured to stop usage, a billing hold, a plan restriction, or a repository Actions policy. The Billing UI and the job's underlying billing/policy annotation were not available through the connected GitHub interface.

## Owner-side steps to clear GitHub-hosted Runner access

1. Open GitHub **Settings → Billing and licensing → Usage**. Inspect Actions included compute, actual usage, account budget, and whether **Stop usage when budget limit is reached** is enabled. Do not raise the budget, enable paid overage, or add a payment method without explicit cost approval.
2. Confirm the account plan supports Actions for private repositories. GitHub documents that Actions is unavailable for private repositories owned by accounts on legacy per-repository plans.
3. Open repository **Settings → Actions → General**. Confirm Actions are enabled and the policy permits standard GitHub-hosted runners and the actions used by the workflow.
4. If usage, plan, and repository policy are all healthy, contact GitHub Support with private runs #970/#971 and public controls #43267300783/#43267582063. Ask why private jobs are rejected before runner assignment and include the `runner_id=0` / empty-steps evidence.

Official references:
- [GitHub Actions billing and usage](https://docs.github.com/en/actions/concepts/billing-and-usage)
- [Troubleshooting workflows — reviewing billing errors](https://docs.github.com/en/actions/how-tos/troubleshoot-workflows)
- [Product usage included with each plan](https://docs.github.com/en/billing/reference/product-usage-included)

## No-hosted-runner workaround added to this branch

A local equivalent of the offline `offline-tests.yml` job is available:

```bash
PYTHON_BIN=python3.14 bash scripts/ci_local_fallback.sh
```

The script:
- requires CPython 3.14 by default; a Python 3.13 run is allowed only with `ALLOW_NON_314=1` and is clearly labeled smoke-only, not Python 3.14 evidence;
- creates a temporary virtual environment and temporary HOME;
- runs project/test processes with an empty environment, so inherited exchange API keys, database credentials, and user tool configuration are not passed to the tested process;
- installs the offline workflow dependencies and the project's declared requirements;
- runs compile checks, rebuilds the source archive, verifies the Render overlay startup through localhost HTTP against a temporary SQLite database, runs the extracted-bundle unittest suite and CLI checks, and runs the Streamlit AppTest;
- restores the original tracked source archive and removes its temporary environment, database, and logs on exit.

For a non-acceptance compatibility smoke run only:

```bash
PYTHON_BIN=python3.13 ALLOW_NON_314=1 bash scripts/ci_local_fallback.sh
```

This local fallback does not run live-provider acceptance and does not write to Production Turso. It makes no exchange order calls. Run it from a trusted Linux, macOS, or WSL checkout of this branch with Bash, tar, curl, grep, and CPython available.

For fully automatic CI without GitHub-hosted private compute, an owner-authorized isolated ephemeral self-hosted runner is another option. Do not attach a long-lived personal machine containing credentials to workflows that execute pull-request code.

## Verification and release boundary

- The new fallback script passed a shell syntax check (`bash -n`) only. It has not been run against a checkout in this environment; no tests are claimed PASS by this recovery change.
- The Billing page and repository Actions settings were not inspectable through the connected session, so the billing/policy sub-cause remains unresolved.
- No payment, budget, plan, repository visibility, runner, secret, Actions setting, merge, or deployment was changed.
- F-49 remains PARTIAL until the exact current HEAD receives actual test evidence. PR #4 remains Draft; PR #1 and `main` remain untouched. `PAPER_ONLY` / `NO_TRADE` and the closed Release Gate remain in force.

## Billing documentation note — checked 2026-11-12

GitHub's current documentation says standard hosted runners are free for public repositories, while private repositories draw runner costs from the account's included monthly compute usage; overage is metered. A budget with **Stop usage when budget limit is reached** can block further hosted-runner use after its applicable budget is exhausted. The included compute allowance is described in monetary terms in the current billing model, so inspect the actual included-usage and budget screens rather than relying only on a historical "minutes remaining" figure.

References:
- https://docs.github.com/en/actions/concepts/billing-and-usage
- https://docs.github.com/en/billing/concepts/product-billing/github-actions
- https://github.com/github/docs/blob/main/content/billing/how-tos/set-up-budgets.md

This documentation confirms that an account-level compute/budget block is a plausible mechanism. It does not identify which, if any, applies to this repository owner; only the account's Billing and Actions settings or GitHub Support can establish that.
