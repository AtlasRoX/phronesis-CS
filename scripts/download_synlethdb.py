"""
Download the SynLethDB human synthetic-lethal pair table for the local SynLethDB evidence source.

    python scripts/download_synlethdb.py [--dest DIR] [--force] [--check]

Source: SynLethDB 3.0 datasets on Zenodo (record 22843223, doi:10.5281/zenodo.22843223, open access), file
Human.SL.detailed.tsv (about 3.7 MB). The default destination is ~/.cache/phronesis/synlethdb. Afterwards set

    PHRONESIS_SYNLETHDB_DIR=<dest>

so phronesis/sources_extra/synlethdb.py can read it. --check only prints the remote file's size (HEAD request).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

URL = "https://zenodo.org/records/22843223/files/Human.SL.detailed.tsv?download=1"
FILE_NAME = "Human.SL.detailed.tsv"
DEFAULT_DEST = Path.home() / ".cache" / "phronesis" / "synlethdb"
EXPECTED_HEADER = ("x_name", "y_name", "rel_source", "pubmed_id")


def head(url: str = URL) -> int:
    with httpx.Client(follow_redirects=True, timeout=30.0) as http:
        resp = http.head(url)
        resp.raise_for_status()
        return int(resp.headers.get("content-length") or 0)


def download(dest: Path, force: bool = False, url: str = URL) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / FILE_NAME
    if target.is_file() and not force:
        print(f"{target} already exists (use --force to replace it)")
        return target
    part = target.with_suffix(target.suffix + ".part")
    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60.0, connect=15.0)) as http:
        with http.stream("GET", url) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length") or 0)
            done = 0
            with open(part, "wb") as fh:
                for chunk in resp.iter_bytes(1 << 16):
                    fh.write(chunk)
                    done += len(chunk)
    if total and done != total:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"incomplete download: {done} of {total} bytes")
    with open(part, encoding="utf-8", errors="replace") as fh:
        header = fh.readline()
    if not all(col in header for col in EXPECTED_HEADER):
        part.unlink(missing_ok=True)
        raise RuntimeError(f"unexpected file header: {header[:200]!r}")
    part.replace(target)
    print(f"Saved {done} bytes to {target}")
    return target


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--dest", type=Path, default=DEFAULT_DEST, help=f"directory (default {DEFAULT_DEST})")
    ap.add_argument("--force", action="store_true", help="replace an existing file")
    ap.add_argument("--check", action="store_true", help="only check the remote file (HEAD) and exit")
    args = ap.parse_args(argv)
    if args.check:
        print(f"{URL}: {head()} bytes")
        return 0
    target = download(args.dest.expanduser(), force=args.force)
    print(f"Set PHRONESIS_SYNLETHDB_DIR={target.parent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
