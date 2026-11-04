"""Prompt texts: the style preset's templates filled with a source's materials, removals and extra wishes."""
from __future__ import annotations

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


def remove_clause(items: list[str], burned: list[str] | None = None) -> str:
    """The removal sentence, then the one for elements the program burns in afterwards (the models leave them out,
    so no garbled copy shows next to the exact one)."""
    texts = read(PROMPTS)
    parts = [texts["remove"].format(items="、".join(items))] if items else []
    if burned:
        parts.append(texts["burn"].format(items="、".join(burned)))
    return "".join(parts)


def burned_items(row: dict) -> list:
    """Overlays the models should leave out: every `overlays` item unless it says "ai": "keep"."""
    return [item for item in row.get("overlays") or [] if item.get("ai", "remove") == "remove"]


def compose(style: dict, kind: str, row: dict, extra: str = "", span: tuple[float, float] | None = None,
            style_ref: bool = False) -> str:
    """kind: anchor (first restyle), keyframe (restyle after the anchor) or video (Seedance edit); `span` is the
    source time the text is for (a frame: start == end). `style_ref`: the anchor gets a texture reference picture as
    its second image (the style's `style_ref` sentence says how to use it)."""
    wishes = " ".join(part.strip() for part in (row.get("extra") or "", extra or "") if part and part.strip())
    materials = "、".join(on_screen(row.get("materials"), span)) or style["default_materials"]
    removal = remove_clause(on_screen(row.get("remove"), span), on_screen(burned_items(row), span))
    text = style[kind].format(materials=materials, remove=removal, look=style.get("look", ""),
                              rebuild=style.get("rebuild", ""), extra=wishes,
                              style_ref=style.get("style_ref", "") if style_ref else "")
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
