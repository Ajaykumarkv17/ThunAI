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

import jsii
from aws_cdk import BundlingOptions, ILocalBundling
from aws_cdk import aws_lambda as lambda_

#: Paths excluded from every Lambda source bundle. `cdk.out` is the one that
#: MUST be here (see module docstring); the rest keep the package small.
#:
#: The patterns are deliberately broad denylist entries: `**/cdk.out` catches a
#: nested cdk output dir at any depth (the recursion source), and the several
#: virtualenv spellings (`.venv`, `venv`, `env`, `virtualenv`, plus a `*venv*`
#: glob) catch a differently-named local environment that would otherwise be
#: copied — hundreds of MB — into every Lambda asset and blow up the bundle.
LAMBDA_ASSET_EXCLUDES: list[str] = [
    # cdk synth output — MUST be excluded at root AND any nested depth, or the
    # asset copies an asset that contains cdk.out ... unbounded recursion.
    "cdk.out",
    "**/cdk.out",
    ".cdk.staging",
    "**/.cdk.staging",
    # VCS / CI
    ".git",
    "**/.git",
    ".github",
    # Node / frontend
    "node_modules",
    "**/node_modules",
    "frontend",
    # Python build/scratch
    "**/__pycache__",
    "**/*.pyc",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "dist",
    "build",
    "*.egg-info",
    # Virtualenvs — cover the common spellings AND a catch-all glob so a
    # differently-named local env is never bundled.
    ".venv",
    "venv",
    "env",
    "virtualenv",
    "**/*venv*",
    # Tests, docs, editor/tooling, local config not needed at runtime.
    "tests",
    "docs",
    ".kiro",
    ".env",
    "*.md",
]


def lambda_source() -> lambda_.AssetCode:
    """Return the Lambda source asset: project code PLUS installed deps.

    The trigger/escalation Lambdas import pydantic-backed modules
    (schemas/*, memory/*, surface/*), so the bundle MUST carry the runtime
    dependencies or the function fails at import with ``No module named
    'pydantic'``. Lambda provides boto3 in its managed runtime, but NOT
    pydantic, so dependencies are pre-installed (for Lambda's platform) into
    ``build/lambda_deps`` and copied into every bundle via local bundling.

    Pre-install step (run once before ``cdk deploy``; also safe to re-run):

        python -m pip install --target build/lambda_deps \\
            --platform manylinux2014_x86_64 --implementation cp \\
            --python-version 3.13 --only-binary=:all: pydantic==2.13.5

    Local bundling (no Docker): the ``command`` runs only if the bundler
    falls back to Docker; ``local`` performs the copy in-process.
    """
    import os
    import shutil

    deps_dir = os.path.abspath(os.path.join("build", "lambda_deps"))

    @jsii.implements(ILocalBundling)
    class _LocalBundle:
        def try_bundle(self, output_dir: str, *, image=None, **_kwargs) -> bool:  # noqa: ANN001
            # Copy the pre-installed dependencies first.
            if os.path.isdir(deps_dir):
                for entry in os.listdir(deps_dir):
                    src = os.path.join(deps_dir, entry)
                    dst = os.path.join(output_dir, entry)
                    if os.path.isdir(src):
                        shutil.copytree(src, dst, dirs_exist_ok=True)
                    else:
                        shutil.copy2(src, dst)
            # Copy the project source (the packages the handlers import).
            for pkg in (
                "agents", "harness", "integrations", "memory", "policy",
                "schemas", "surface", "tools", "triggers", "seed",
            ):
                if os.path.isdir(pkg):
                    shutil.copytree(
                        pkg, os.path.join(output_dir, pkg), dirs_exist_ok=True
                    )
            return True

    return lambda_.Code.from_asset(
        ".",
        exclude=list(LAMBDA_ASSET_EXCLUDES),
        bundling=BundlingOptions(
            image=lambda_.Runtime.PYTHON_3_13.bundling_image,
            command=[
                "bash",
                "-c",
                "pip install -r requirements.txt -t /asset-output && cp -r "
                "agents harness integrations memory policy schemas surface tools "
                "triggers seed /asset-output/",
            ],
            local=_LocalBundle(),
        ),
    )