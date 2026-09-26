"""Build the Chrome Web Store upload: dist/coriqo-shield-<version>.zip.

    python scripts/package_extension.py            # store upload (no "key")
    python scripts/package_extension.py --keep-key # dev build with the fixed ID

The manifest's "key" pins a development ID (see README). The store assigns its
own ID, so the upload leaves "key" out; after the first upload, copy the public
key from the dashboard (Package -> View public key) into the manifest so the
published and unpacked copies share one ID.
"""
from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "byoai" / "browser_extension"
# Only what an extension ships; stray files (.DS_Store, drafts, caches) stay out.
SHIP = {".js", ".html", ".json", ".md", ".png", ".css"}


def build(out_dir: Path, keep_key: bool = False) -> Path:
    manifest = json.loads((SRC / "manifest.json").read_text())
    if not keep_key:
        manifest.pop("key", None)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"coriqo-shield-{manifest['version']}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(SRC.rglob("*")):
            if path.is_dir() or path.suffix not in SHIP or path.name.startswith(".") or "__pycache__" in path.parts:
                continue
            rel = path.relative_to(SRC).as_posix()
            if rel == "manifest.json":
                zf.writestr(rel, json.dumps(manifest, indent=2) + "\n")
            else:
                zf.write(path, rel)
    return target


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="dist", help="output directory (default: dist)")
    ap.add_argument("--keep-key", action="store_true", help='keep the manifest "key" (development ID)')
    args = ap.parse_args()
    print(build(Path(args.out), args.keep_key))


if __name__ == "__main__":
    main()
