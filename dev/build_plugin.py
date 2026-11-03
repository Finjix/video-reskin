"""Validate the complete upload folder and produce the marketplace ZIP."""
import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin"


def validate(root=PLUGIN):
    metadata = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    assert metadata["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    assert metadata["name"] == "video-reskin"
    interface = metadata["extensions"]["com.openai"]["interface"]
    assert metadata["extensions"]["com.openai"]["requires_local_executor"] is True
    for key in ("displayName", "shortDescription"):
        assert 0 < len(interface[key]) <= 30, key
    assert interface["developerName"] and interface["longDescription"] and interface["category"]
    for field in ("logo", "composerIcon"):
        path = interface[field]
        assert path.startswith("./") and ".." not in Path(path).parts
        icon = root / path
        assert icon.is_file() and icon.stat().st_size <= 5 * 1024**2
        svg = ElementTree.parse(icon).getroot()
        assert svg.tag.endswith("svg")
        assert float(svg.attrib["width"]) == float(svg.attrib["height"]) >= 48
    skill = root / "skills/video-reskin/SKILL.md"
    instructions = skill.read_text(encoding="utf-8")
    assert instructions.startswith("---\nname: video-reskin\n") and "description:" in instructions
    assert (skill.parent / "agents/openai.yaml").is_file()
    # The runtime cache (downloaded on first use, Git-ignored) is never part of the upload.
    files = sorted(path for path in root.rglob("*") if path.is_file() and path.relative_to(root).parts[0] != "runtime-cache")
    assert len(files) <= 5000
    assert sum(path.stat().st_size for path in files) <= 512 * 1024**2
    names = set()
    for file in files:
        relative = file.relative_to(root)
        assert not file.is_symlink()
        assert "__pycache__" not in relative.parts and "dependencies" not in relative.parts
        assert file.suffix.lower() not in (".exe", ".dll", ".pyd", ".pyc", ".npz", ".safetensors", ".partial")
        assert len(relative.parts) <= 19 and file.stat().st_size <= 100 * 1024**2
        key = relative.as_posix().casefold()
        assert key not in names
        names.add(key)
    return metadata, files


def main():
    metadata, files = validate()
    output = ROOT / "dist" / f"{metadata['name']}-{metadata['version']}.zip"
    output.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            archive.write(path, "plugin/" + path.relative_to(PLUGIN).as_posix())
    assert output.stat().st_size <= 100_000_000
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        assert len({name.split('/')[0] for name in archive.namelist()}) == 1
    print(json.dumps({"zip": str(output), "files": len(files), "bytes": output.stat().st_size}))


if __name__ == "__main__":
    main()
