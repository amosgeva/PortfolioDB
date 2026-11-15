"""Print the SLSA v1 provenance predicate publish.yml attests for an image.

Why not the stock `actions/attest-build-provenance`: it builds its predicate
from the job's OIDC claims, so its only source dependency is the commit the
*workflow* ran from. For a push or a tag that is also the commit the image was
built from. For the weekly rebuild it is not: the workflow runs from main's
head (commit A) and builds the commit `latest` already points at, or the
newest release tag (commit B). The stock predicate then named A and nothing
else, so a consumer of the signed provenance could not tell which application
source the image holds (QA of PR #124, defect 1).

This writes the same predicate the stock action writes -- same build type,
external and internal parameters, builder id and invocation, all taken from
the same values (the runner's GITHUB_* variables carry the claims the OIDC
token is minted from) -- and adds the source that was actually built as a
second resolved dependency. Who built it (the builder id and the workflow
dependency) stays exactly as truthful as before; what was built is now in the
signed statement too. publish.yml hands the output to actions/attest, which
signs it unchanged.

    python3 provenance_predicate.py --built-commit "$(git rev-parse HEAD)" --built-ref v1.7.7

Standard library only: publish.yml extracts this file from the workflow's own
commit and runs it beside a checkout of an older release, which may predate it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

PREDICATE_TYPE = "https://slsa.dev/provenance/v1"
# The build type the stock action declares; unchanged, because the build is
# still exactly a GitHub Actions workflow run.
GITHUB_BUILD_TYPE = "https://actions.github.io/buildtypes/workflow/v1"

_REQUIRED_ENV = (
    "GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_REPOSITORY_ID", "GITHUB_REPOSITORY_OWNER_ID",
    "GITHUB_REF", "GITHUB_SHA", "GITHUB_WORKFLOW_REF", "GITHUB_EVENT_NAME",
    "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "RUNNER_ENVIRONMENT",
)
_SHA = re.compile(r"^[0-9a-f]{40}$")


def _source_uri(server: str, repository: str, ref: str) -> str:
    """`git+<repo>@<ref>`, with a bare release tag spelled as the full ref."""
    if re.fullmatch(r"v\d+\.\d+\.\d+.*", ref):
        ref = f"refs/tags/{ref}"
    return f"git+{server}/{repository}@{ref}"


def build_predicate(env: dict[str, str], built_commit: str, built_ref: str) -> dict:
    missing = [k for k in _REQUIRED_ENV if not env.get(k)]
    if missing:
        raise ValueError("missing GitHub Actions environment: " + ", ".join(missing))
    if not _SHA.fullmatch(built_commit):
        raise ValueError(f"--built-commit must be a full 40-character commit SHA, got {built_commit!r}")

    server, repo = env["GITHUB_SERVER_URL"], env["GITHUB_REPOSITORY"]
    workflow_ref = env["GITHUB_WORKFLOW_REF"]            # owner/repo/.github/workflows/x.yml@refs/...
    workflow_path = workflow_ref.replace(f"{repo}/", "", 1).split("@", 1)[0]

    return {
        "buildDefinition": {
            "buildType": GITHUB_BUILD_TYPE,
            "externalParameters": {
                "workflow": {
                    "ref": env["GITHUB_REF"],
                    "repository": f"{server}/{repo}",
                    "path": workflow_path,
                }
            },
            "internalParameters": {
                "github": {
                    "event_name": env["GITHUB_EVENT_NAME"],
                    "repository_id": env["GITHUB_REPOSITORY_ID"],
                    "repository_owner_id": env["GITHUB_REPOSITORY_OWNER_ID"],
                    "runner_environment": env["RUNNER_ENVIRONMENT"],
                }
            },
            "resolvedDependencies": [
                {
                    # What the stock action records: the workflow's own source.
                    "uri": f"git+{server}/{repo}@{env['GITHUB_REF']}",
                    "digest": {"gitCommit": env["GITHUB_SHA"]},
                    "name": "workflow",
                },
                {
                    # The application source the image was built from: the
                    # publish job's checkout, `git rev-parse HEAD`.
                    "uri": _source_uri(server, repo, built_ref),
                    "digest": {"gitCommit": built_commit},
                    "name": "source",
                    "annotations": {"role": "the commit the image was built from"},
                },
            ],
        },
        "runDetails": {
            # The stock action uses the job_workflow_ref claim; for a job
            # defined in the workflow file itself (publish.yml's are) it is the
            # workflow ref.
            "builder": {"id": f"{server}/{workflow_ref}"},
            "metadata": {
                "invocationId": (
                    f"{server}/{repo}/actions/runs/{env['GITHUB_RUN_ID']}/attempts/{env['GITHUB_RUN_ATTEMPT']}"
                )
            },
        },
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--built-commit", required=True, help="full SHA of the checkout the image was built from")
    ap.add_argument("--built-ref", required=True, help="the ref that checkout was made from (tag, branch or SHA)")
    args = ap.parse_args(argv)
    try:
        predicate = build_predicate(dict(os.environ), args.built_commit, args.built_ref)
    except ValueError as exc:
        print(f"provenance_predicate: {exc}", file=sys.stderr)
        return 1
    json.dump(predicate, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
