"""Prompt texts: the style preset's templates filled with a source's materials, removals and extra wishes."""
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


removals = on_screen


def remove_clause(items: list[str]) -> str:
    if not items:
        return ""
    return read(PROMPTS)["remove"].format(items=", ".join(items))


def compose(style: dict, kind: str, row: dict, extra: str = "", span: tuple[float, float] | None = None,
            refs: str = "", **fields) -> str:
    """kind: scene (the scene map), subject (a three-view sheet) or video (the H3 edit); `span` is the source time
    the text is for; `refs` says what the reference pictures are (video only; placed at {refs}, or after the
    template when it has none); `fields` fill the kind's own placeholders ({views}, {look_image}, {what}...)."""
    wishes = " ".join(part.strip() for part in (row.get("extra") or "", extra or "") if part and part.strip())
    materials = ", ".join(on_screen(row.get("materials"), span)) or style["default_materials"]
    base = style.get(kind) or read(PROMPTS)["presets"]["realistic"][kind]    # custom styles may lack a kind
    template = base if "{refs}" in base or not refs else base + " {refs}"
    text = template.format(materials=materials, remove=remove_clause(on_screen(row.get("remove"), span)),
                           look=style.get("look", ""), extra=wishes, refs=refs, **fields)
    return " ".join(text.split())


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
