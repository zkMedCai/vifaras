"""Remove the legacy `.secrets/agent_keys/` directory after the [7.4.1] migration.

V0 [7.4.1] flipped per-agent ed25519 keypair custody from filesystem
(`file:<path>` kms_ref → JSON files under `.secrets/agent_keys/`) to DB
(`db:<id>` kms_ref → AES-256-GCM-encrypted rows in `kms_agent_keys`).

After the schema migration is applied AND `Agent.privkey_kms_ref` rows pointing
at the legacy `file:` scheme have been re-issued (drop+recreate strategy: the
founder reruns tier upgrade), the on-disk JSON files are dead weight. This
script removes them, logs the file count for audit, and leaves the directory
absent.

Idempotent: re-running after the directory is already gone is a no-op.

Usage:
    uv run python scripts/cleanup_legacy_kms_keys.py
"""
from __future__ import annotations

import shutil
from pathlib import Path

from app.core.logging import configure_logging, log


_LEGACY_DIR = Path(".secrets/agent_keys")


def main() -> None:
    configure_logging()

    if not _LEGACY_DIR.exists():
        log.info("legacy_kms_cleanup.absent", path=str(_LEGACY_DIR))
        return

    file_count = sum(1 for _ in _LEGACY_DIR.glob("*.json"))
    shutil.rmtree(_LEGACY_DIR, ignore_errors=True)
    log.info(
        "legacy_kms_cleanup.complete",
        removed_files=file_count,
        path=str(_LEGACY_DIR),
    )


if __name__ == "__main__":
    main()
