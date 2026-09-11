"""arm64 pre-create architecture guard (Req 19.2, design.md §3.11).

``verify_arm64_or_abort(package_dir)`` inspects every compiled shared object
(``*.so``) in a built AgentCore Runtime bundle and aborts ``cdk synth``
*before* the ``CfnRuntime`` L1 is instantiated if any of them targets an
architecture other than arm64 (AArch64). This turns an architecture mismatch
into a local, immediate failure with a concrete remediation command rather
than a ``CREATE_FAILED`` reached only after the resource hits the AWS API.

The check is pure-Python ELF header inspection — no external tooling. The
ELF ``e_machine`` field lives at byte offset 18 (little-endian half-word);
``AArch64 == 0xB7`` per the ELF ABI.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Final

#: ELF ``e_machine`` value for the AArch64 (arm64) architecture.
E_MACHINE_AARCH64: Final[int] = 0xB7

#: Byte offset of the ``e_machine`` half-word within an ELF header.
_E_MACHINE_OFFSET: Final[int] = 18

#: The four-byte ELF magic every valid ELF file begins with (0x7F 'E' 'L' 'F').
_ELF_MAGIC: Final[bytes] = b"\x7fELF"


def _elf_machine(path: Path) -> str:
    """Return a human-readable architecture name for one ELF file.

    Reads the ELF header's ``e_machine`` field. A file that is not a valid
    ELF object (wrong magic / too short) is reported as ``"not-elf"`` so the
    caller can treat it as non-arm64 and surface it rather than crashing.
    """
    try:
        with open(path, "rb") as handle:
            header = handle.read(20)
    except OSError:
        return "unreadable"

    if len(header) < _E_MACHINE_OFFSET + 2 or header[:4] != _ELF_MAGIC:
        return "not-elf"

    e_machine = int.from_bytes(
        header[_E_MACHINE_OFFSET : _E_MACHINE_OFFSET + 2], "little"
    )
    return "AArch64" if e_machine == E_MACHINE_AARCH64 else f"other(0x{e_machine:x})"


def verify_arm64_or_abort(package_dir: str) -> None:
    """Abort ``cdk synth`` if any built shared object is not arm64 (Req 19.2).

    Called as the first line of ``AgentCoreStack.__init__`` — before the
    ``CfnRuntime`` L1 is instantiated — so a bad build fails locally with the
    exact rebuild command instead of reaching ``CREATE_FAILED`` in AWS.

    A missing/empty ``package_dir`` is treated as "nothing to verify" and
    returns cleanly, so a synth without a pre-built bundle (e.g. a ``cdk
    diff`` on a machine that has not yet run the packaging step) is not
    blocked; the architecture guarantee is enforced whenever compiled
    artefacts are actually present.
    """
    root = Path(package_dir)
    if not root.exists():
        return

    so_files = list(root.rglob("*.so"))
    bad = [f for f in so_files if _elf_machine(f) != "AArch64"]
    if not bad:
        return

    example = bad[0]
    print(
        f"ARCHITECTURE MISMATCH: {len(bad)} non-arm64 shared object(s) found in "
        f"{package_dir}, e.g. {example} ({_elf_machine(example)}).\n"
        f"AgentCore Runtime requires arm64. Rebuild the bundle with:\n"
        f"  uv pip install --python-platform aarch64-manylinux2014 "
        f"--python-version 3.13 --target={package_dir} --only-binary=:all: "
        f"-r requirements.txt",
        file=sys.stderr,
    )
    sys.exit(1)
