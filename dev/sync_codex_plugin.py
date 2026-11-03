"""Bring the Codex-installed copy of the plugin up to date with the repository's `plugin/` folder.

Codex installs plugins under ~/.codex/plugins/cache/<marketplace>/video-reskin/<version>/. Fixes in the
repository reach jobs only after that copy is refreshed. Every Git-tracked file under plugin/ is copied over,
files the repository no longer has are removed, and the installed runtime-cache/ (downloaded dependencies and
models) is left alone. Run with --dry-run to list the changes only.
"""
import argparse
import filecmp
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin"
KEEP = {"runtime-cache", ".codex-plugin"}


def installs() -> list[Path]:
    cache = Path.home() / ".codex/plugins/cache"
    return sorted(path for path in cache.glob("*/video-reskin/*") if (path / "plugin.json").is_file())


def tracked() -> list[str]:
    output = subprocess.run(["git", "ls-files", "-z", "plugin"], cwd=ROOT, capture_output=True, check=True).stdout
    return sorted(name[len("plugin/"):] for name in output.decode("utf-8").split("\0") if name)


def codex_manifest(metadata: dict) -> dict:
    """The manifest Codex wrote at install time: the OpenAI extension flattened beside the plugin fields."""
    return {**metadata["extensions"]["com.openai"], "name": metadata["name"], "version": metadata["version"],
            "description": metadata["description"], "author": metadata["author"],
            "keywords": metadata.get("keywords", []), "skills": "./skills"}


def sync(target: Path, files: list[str], dry_run: bool) -> dict:
    changed, removed = [], []
    for name in files:
        source, destination = PLUGIN / name, target / name
        if destination.is_file() and filecmp.cmp(source, destination, shallow=False):
            continue
        changed.append(name)
        if not dry_run:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    wanted = set(files)
    for path in sorted(target.rglob("*")):
        relative = path.relative_to(target)
        if relative.parts[0] in KEEP or not path.is_file():
            continue
        if relative.as_posix() not in wanted:
            removed.append(relative.as_posix())
            if not dry_run:
                path.unlink()
    if not dry_run:
        for folder in sorted((p for p in target.rglob("*") if p.is_dir() and p.relative_to(target).parts[0] not in KEEP),
                             key=lambda p: len(p.parts), reverse=True):
            if not any(folder.iterdir()):
                folder.rmdir()
    manifest = target / ".codex-plugin/plugin.json"
    expected = codex_manifest(json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8")))
    current = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else None
    if current != expected:
        changed.append(".codex-plugin/plugin.json")
        if not dry_run:
            manifest.parent.mkdir(exist_ok=True)
            manifest.write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
    return {"install": str(target), "updated": changed, "removed": removed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    targets = installs()
    if not targets:
        raise SystemExit("no Codex-installed video-reskin plugin found under ~/.codex/plugins/cache")
    version = json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))["version"]
    files = tracked()
    for target in targets:
        if target.name != version:
            print(f"note: {target} is installed as version {target.name}; the repository is {version}")
        print(json.dumps(sync(target, files, args.dry_run), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
