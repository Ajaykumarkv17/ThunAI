"""``python -m evals`` entry point — runs the recorded scenario suite.

Delegates to ``evals.harness._main`` so the CLI is available both as
``python -m evals`` (no import-order warning) and ``python -m evals.harness``.
"""

from __future__ import annotations

from evals.harness import _main

if __name__ == "__main__":
    raise SystemExit(_main())
