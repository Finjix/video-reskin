"""Prompt texts: the style preset's templates filled with a source's materials, removals and extra wishes."""
from __future__ import annotations

from reskin.common import PROMPTS, read


def on_screen(items, span: tuple[float, float] | None) -> list[str]:
    """The texts of `items` that apply to a moment or stretch of the source (an item may carry start/end seconds).

    Naming something that is not on screen makes the models draw it (a truck listed for the end of an ad appears in
    its first seconds; text listed for removal turns up as garbled text), so timed items are left out elsewhere.
    """
    return list(dict.fromkeys(item if isinstance(item, str) else item["what"] for item in timed(items, span)))


def timed(items, span: tuple[float, float] | None) -> list:
    """The items (texts or dicts) on screen during `span` (all of them without a span)."""
    if isinstance(items, str):
        items = [items]
    return [item for item in items or [] if isinstance(item, str) or span is None
            or (item.get("start", 0) <= span[1] and span[0] <= item.get("end", float("inf")))]


removals = on_screen


def is_wiped(item) -> bool:
    """A `remove` item the program wipes from the clip Seedance gets: one with a `box`, unless `"wipe": "none"`."""
    return isinstance(item, dict) and bool(item.get("box")) and item.get("wipe", "before") == "before"


def where(box: list[float]) -> str:
    """Where a box sits in the frame, in the words of a prompt: 右上角, 底部, 左侧..."""
    x, y = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    vertical = "上" if y < 1 / 3 else "下" if y > 2 / 3 else ""
    horizontal = "" if box[2] - box[0] > .5 else "左" if x < 1 / 3 else "右" if x > 2 / 3 else ""
    if vertical and horizontal:
        return f"{horizontal}{vertical}角"
    if vertical:
        return "顶部" if vertical == "上" else "底部"
    return f"{horizontal}侧" if horizontal else "中间"


def remove_clause(items: list[str], burned: list[str] | None = None) -> str:
    """The removal sentence, then the one for elements the program burns in afterwards (the models leave them out,
    so no garbled copy shows next to the exact one)."""
    texts = read(PROMPTS)
    parts = [texts["remove"].format(items="、".join(items))] if items else []
    if burned:
        parts.append(texts["burn"].format(items="、".join(burned)))
    return "".join(parts)


def wiped_clause(items: list, span: tuple[float, float] | None) -> str:
    """For the video prompt: where the program wiped logos from the clip, by position only. Seedance kept the smooth
    fill as a blurred patch (S01/S09 of the 1080p batch, every corner); naming the logo instead made it draw one."""
    spots = list(dict.fromkeys(where(item["box"]) for item in timed([i for i in items if is_wiped(i)], span)))
    return read(PROMPTS).get("wiped", "").format(where="、".join(spots)) if spots else ""


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
    items = row.get("remove") or []
    if kind == "video":
        # Wiped items stay unnamed for Seedance (naming a logo made it draw one, even over a wiped corner); the
        # prompt only says where the wiped patches are, so it paints scene there instead of keeping the smear.
        told = [i for i in items if not is_wiped(i)]
        removal = remove_clause(on_screen(told, span), on_screen(burned_items(row), span)) + wiped_clause(items, span)
    else:
        # GPT Image sees the original frame, logo included, and removes what it is told to.
        removal = remove_clause(on_screen(items, span), on_screen(burned_items(row), span))
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
