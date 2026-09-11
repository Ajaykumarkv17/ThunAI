"""Shared Lambda source-asset bundler for ThunAI CDK stacks.

Every trigger/escalation/realtime Lambda ships the project root as its source
bundle. `lambda_.Code.from_asset(".")` with no exclusions is a trap: CDK stages
the bundle into `cdk.out/asset.<hash>/`, and since that copy includes `cdk.out`
itself, the next synth copies an asset that contains an asset that contains
cdk.out ... an unbounded `cdk.out/asset.<hash>/cdk.out/asset.<hash>/...`
recursion that eventually blows past Windows' path-length limit and fails
`cdk synth` with `ENOENT ... mkdir`.

`lambda_source()` bundles the same project root but excludes `cdk.out` (the
recursion source) plus the other build/scratch/test dirs that have no business
in a Lambda package. One definition, used by every stack, so the exclusion set
can never drift between them.
"""

from __future__ import annotations

from aws_cdk import aws_lambda as lambda_

#: Paths excluded from every Lambda source bundle. `cdk.out` is the one that
#: MUST be here (see module docstring); the rest keep the package small.
LAMBDA_ASSET_EXCLUDES: list[str] = [
    "cdk.out",
    ".git",
    ".github",
    "node_modules",
    "frontend",
    "**/__pycache__",
    "**/*.pyc",
    ".venv",
    "venv",
    ".pytest_cache",
    ".ruff_cache",
    "tests",
    "dist",
    ".env",
]


def lambda_source() -> lambda_.AssetCode:
    """Return the project-root Lambda source asset with safe exclusions."""
    return lambda_.Code.from_asset(".", exclude=list(LAMBDA_ASSET_EXCLUDES))