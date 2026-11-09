"""Check that the repository root is a valid, distributable Agent Skill.

The root folder is the skill: SKILL.md beside references/, scripts/, assets/ and requirements/. Zip the root
folder to share it (leave out .git/, dev/ and the downloaded runtime-cache/).
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "video-reskin"
PARTS = ("SKILL.md", "references", "scripts", "assets", "requirements")


def skill_files(root=ROOT) -> list[Path]:
    found = []
    for part in PARTS:
        path = root / part
        found.extend([path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file()))
    return [p for p in found if "__pycache__" not in p.parts and p.suffix != ".pyc"]


def validate(root=ROOT) -> list[Path]:
    text = (root / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"---\nname: ([a-z0-9-]+)\ndescription: (.+?)\n---\n", text, re.S)
    assert match and match.group(1) == NAME, "SKILL.md frontmatter"
    assert 0 < len(match.group(2)) <= 1024, "description must be 1-1024 characters"
    for link in re.findall(r"\]\(([^)#:]+)\)", text):
        assert (root / link).is_file(), f"SKILL.md links a missing file: {link}"
    files = skill_files(root)
    assert sum(path.stat().st_size for path in files) <= 512 * 1024**2
    names = set()
    for file in files:
        relative = file.relative_to(root)
        assert not file.is_symlink()
        assert file.suffix.lower() not in (".exe", ".dll", ".pyd", ".npz", ".safetensors", ".partial"), relative
        assert file.stat().st_size <= 100 * 1024**2, relative
        key = relative.as_posix().casefold()
        assert key not in names
        names.add(key)
    return files


if __name__ == "__main__":
    files = validate()
    print(json.dumps({"skill": NAME, "files": len(files), "bytes": sum(p.stat().st_size for p in files)}))
