#!/usr/bin/env python3
"""
Archive Codebase Utility
Creates and maintains a clean, production-grade ZIP archive of the Phronesis codebase.
Automatically excludes transient files, virtual environments, build artifacts, and caches.
"""

import os
import sys
import zipfile
import fnmatch
import argparse
import shutil
import tempfile
from pathlib import Path
from datetime import datetime

# Default directories to ignore
IGNORED_DIRS = {
    ".venv",
    "venv",
    "env",
    "node_modules",
    ".next",
    "__pycache__",
    ".git",
    ".phronesis_memory",
    ".phronesis_runs",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".idea",
    ".vscode",
    "coverage",
    ".turbo",
    "dist",
    "build",
    ".cache",
    "tmp",
    "temp",
    "logs",
    "eval_runs",
}

# Default file patterns to ignore (secrets, runtime databases, temp files, caches)
IGNORED_FILE_PATTERNS = [
    "*.zip",
    "*.tmp",
    "*.pyc",
    "*.pyo",
    "*.pyd",
    "*.tsbuildinfo",
    ".DS_Store",
    "Thumbs.db",
    "*.log",
    # Secrets and environment files
    ".env",
    ".env.*",
    "*.env",
    "*credentials*",
    "*.pem",
    "*.key",
    "id_rsa*",
    # Runtime databases & caches
    "*.db",
    "*.sqlite",
    "*.sqlite3",
    "*.pid",
    "*.bak",
]

# Patterns ignored only when --slim is specified
SLIM_EXCLUDE_PATTERNS = [
    "*.pkl",
    "*.parquet",
    "*.parquet.gzip",
    "*.gz",
    "*.tar",
    "*.h5",
    "*.pth",
    "*.pt",
]


if hasattr(sys.stdout, "reconfigure"):
    getattr(sys.stdout, "reconfigure")(encoding="utf-8", errors="replace")


def should_exclude_dir(dir_name: str) -> bool:
    return dir_name in IGNORED_DIRS or dir_name.startswith(".venv")


def should_exclude_file(file_name: str, slim: bool = False) -> bool:
    for pattern in IGNORED_FILE_PATTERNS:
        if fnmatch.fnmatch(file_name, pattern):
            return True
    if slim:
        for pattern in SLIM_EXCLUDE_PATTERNS:
            if fnmatch.fnmatch(file_name, pattern):
                return True
    return False


def create_codebase_zip(root_dir: Path, output_zip: Path, slim: bool = False) -> dict:
    root_dir = root_dir.resolve()
    output_zip = output_zip.resolve()

    # Ensure parent dir exists
    output_zip.parent.mkdir(parents=True, exist_ok=True)

    # Temporary zip file in system temp to avoid target disk space exhaustion
    temp_dir = Path(tempfile.gettempdir())
    temp_zip = temp_dir / f"{output_zip.stem}_{os.getpid()}_{int(datetime.now().timestamp())}.tmp.zip"
    if temp_zip.exists():
        temp_zip.unlink()

    included_count = 0
    total_uncompressed_bytes = 0

    mode_label = "SLIM (Code & Configs)" if slim else "FULL (Code, Notebooks & Datasets)"
    print(f"[*] Packaging codebase: {mode_label}")
    print(f"    Source root: {root_dir}")
    print(f"    Destination: {output_zip}")

    try:
        with zipfile.ZipFile(temp_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zipf:
            for root, dirs, files in os.walk(root_dir):
                current_path = Path(root)

                # Filter out ignored directories in-place so os.walk does not descend into them
                dirs[:] = [d for d in dirs if not should_exclude_dir(d)]

                for file in sorted(files):
                    if should_exclude_file(file, slim=slim):
                        continue

                    full_path = current_path / file
                    # Do not include the output zip itself or temporary file
                    if full_path in (output_zip, temp_zip):
                        continue

                    rel_path = full_path.relative_to(root_dir)

                    try:
                        file_stat = full_path.stat()
                        zipf.write(full_path, arcname=str(rel_path).replace("\\", "/"))
                        included_count += 1
                        total_uncompressed_bytes += file_stat.st_size
                    except (PermissionError, FileNotFoundError) as err:
                        print(f"[-] Skipping {rel_path}: {err}")

        # Move to destination atomically
        if output_zip.exists():
            output_zip.unlink()
        shutil.move(str(temp_zip), str(output_zip))
    finally:
        if temp_zip.exists():
            try:
                temp_zip.unlink()
            except OSError:
                pass

    archive_size_bytes = output_zip.stat().st_size

    stats = {
        "file_count": included_count,
        "uncompressed_bytes": total_uncompressed_bytes,
        "archive_size_bytes": archive_size_bytes,
        "archive_path": str(output_zip),
        "slim": slim,
        "timestamp": datetime.now().isoformat(),
    }

    print("\n[+] Codebase archive created successfully!")
    print(f"    - Mode:                 {mode_label}")
    print(f"    - Total Files:          {included_count}")
    print(f"    - Uncompressed Size:    {total_uncompressed_bytes / (1024 * 1024):.2f} MB")
    print(f"    - Compressed ZIP Size:  {archive_size_bytes / (1024 * 1024):.2f} MB")
    print(f"    - Compression Ratio:    {(1 - archive_size_bytes / max(1, total_uncompressed_bytes)) * 100:.1f}%")
    print(f"    - Output File:          {output_zip}\n")

    return stats


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent

    parser = argparse.ArgumentParser(
        description="Creates and maintains clean ZIP archives of the Phronesis codebase."
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=None,
        help="Path to output zip file (default: phronesis_codebase.zip in repo root)",
    )
    parser.add_argument(
        "-s", "--slim",
        action="store_true",
        help="Exclude heavy test datasets, embeddings (.pkl, .parquet) for a lightweight distribution archive",
    )
    parser.add_argument(
        "--both",
        action="store_true",
        help="Generate both full (phronesis_codebase.zip) and slim (phronesis_codebase_slim.zip) archives",
    )

    args = parser.parse_args()

    if args.both:
        full_zip = repo_root / "phronesis_codebase.zip"
        slim_zip = repo_root / "phronesis_codebase_slim.zip"
        create_codebase_zip(repo_root, full_zip, slim=False)
        create_codebase_zip(repo_root, slim_zip, slim=True)
    else:
        if args.output:
            target_zip = args.output
        else:
            filename = "phronesis_codebase_slim.zip" if args.slim else "phronesis_codebase.zip"
            target_zip = repo_root / filename
        create_codebase_zip(repo_root, target_zip, slim=args.slim)


if __name__ == "__main__":
    main()
