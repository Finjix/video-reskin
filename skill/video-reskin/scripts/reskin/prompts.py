"""Prompt texts: the style preset's templates (assets/prompts.json) filled with a source's materials, removals and
extra wishes, and the sentences that say what each reference picture is."""
from __future__ import annotations

from pathlib import Path

from reskin.common import PROMPTS, read


def on_screen(items, span: tuple[float, float] | None) -> list[str]:
    """The texts of `items` that apply to a moment or stretch of the source (an item may carry start/end seconds).

    Naming something that is not on screen makes the models draw it (a truck listed for the end of an ad appears in
    its first seconds; text listed for removal turns up as garbled text), so timed items are left out elsewhere.
    """
    if isinstance(items, str):
        items = [items]
    chosen = []
    for item in items or []:
        if isinstance(item, str):
            chosen.append(item)
        elif span is None or (item.get("start", 0) <= span[1] and span[0] <= item.get("end", float("inf"))):
            chosen.append(item["what"])
    return list(dict.fromkeys(chosen))


def preset_text(style: dict, key: str):
    """The style's own text for `key`, else the realistic preset's (a custom style may define only some)."""
    return style.get(key) or read(PROMPTS)["presets"]["realistic"][key]


def remove_clause(items: list[str]) -> str:
    if not items:
        return ""
    return read(PROMPTS)["remove"].format(items="; ".join(items))


def compose(style: dict, kind: str, row: dict, extra: str = "", span: tuple[float, float] | None = None,
            refs: str = "", **fields) -> str:
    """kind: scene (the scene map), subject (a three-view sheet) or video (the Seedance edit); `span` is the source time
    the text is for; `refs` says what the reference pictures are (video only; placed at {refs}, or after the
    template when it has none); `fields` fill the kind's own placeholders ({views}, {look_image}, {what}...)."""
    wishes = " ".join(part.strip() for part in (row.get("extra") or "", extra or "") if part and part.strip())
    materials = ", ".join(on_screen(row.get("materials"), span)) or style["default_materials"]
    base = preset_text(style, kind)
    template = base if "{refs}" in base or not refs else base + " {refs}"
    text = template.format(materials=materials, remove=remove_clause(on_screen(row.get("remove"), span)),
                           look=style.get("look", ""), ground=style.get("ground", ""), extra=wishes, refs=refs,
                           **fields)
    return " ".join(text.split())


def references_clause(style: dict, chosen: list[dict]) -> str:
    """What each reference picture of a video edit is for, in order (Image 1, 2, ...): the previous result's frame
    (role "previous"), scene maps ("scene"), three-view sheets ("subject", with `what`)."""
    texts = preset_text(style, "references")
    parts = [texts["previous"].format(n=n) for n, p in enumerate(chosen, 1) if p["role"] == "previous"]
    scenes = [n for n, p in enumerate(chosen, 1) if p["role"] == "scene"]
    if scenes:
        images = (f"Image {scenes[0]}" if len(scenes) == 1
                  else "Images " + ", ".join(map(str, scenes[:-1])) + f" and {scenes[-1]}")
        parts.append(texts["scene"].format(images=images))
    parts += [texts["subject"].format(n=n, what=p["what"]) for n, p in enumerate(chosen, 1) if p["role"] == "subject"]
    if parts:
        parts.append(texts["closing"])
    return " ".join(parts)


def describe(items) -> str:
    """Materials or removals for reports: `what` with its time range when it has one."""
    if isinstance(items, str):
        return items
    parts = []
    for item in items or []:
        if isinstance(item, str):
            parts.append(item)
        else:
            parts.append(f"{item['what']} ({item.get('start', 0):g}-{item['end']:g} s)" if "end" in item
                         else f"{item['what']} (from {item.get('start', 0):g} s)")
    return "; ".join(parts)


def reference(style: dict) -> Path | None:
    """The style's look reference picture (sent with every scene map as its last image), if it has one; relative to assets/."""
    value = style.get("reference")
    if not value:
        return None
    path = Path(value) if Path(value).is_absolute() else PROMPTS.parent / value
    if not path.is_file():
        raise FileNotFoundError(f"style reference picture not found: {path}")
    return path
