"""Collapsible JSON tree for the GUI (V1-17).

Everything that reaches the output goes through ``markupsafe`` escaping, so the returned
``Markup`` is safe to place in a template even for hostile input (JSON-LD from a fetched page).
Hard limits bound both output size and recursion: ``MAX_DEPTH`` levels, ``MAX_NODES`` nodes in
total and ``MAX_STRING`` characters per string; anything past a limit becomes a marker.
"""

from collections.abc import Mapping

from markupsafe import Markup, escape

MAX_DEPTH = 20
MAX_NODES = 5000
MAX_STRING = 2000
OPEN_DEPTH = 2
"""Containers at a depth below this start expanded; deeper ones start collapsed."""

_TRUNCATED = Markup('<li class="trunc muted">… truncated</li>')


class _Budget:
    def __init__(self) -> None:
        self.left = MAX_NODES

    def take(self) -> bool:
        if self.left <= 0:
            return False
        self.left -= 1
        return True


def _text(value: object) -> Markup:
    s = value if isinstance(value, str) else str(value)
    extra = len(s) - MAX_STRING
    if extra > 0:
        return Markup('{}<span class="muted">… ({} more chars)</span>').format(
            s[:MAX_STRING], extra
        )
    return escape(s)


def _scalar(value: object) -> Markup:
    if value is None:
        return Markup('<span class="v v-null">null</span>')
    if isinstance(value, bool):
        return Markup('<span class="v v-bool">{}</span>').format("true" if value else "false")
    if isinstance(value, int | float):
        return Markup('<span class="v v-num">{}</span>').format(value)
    if isinstance(value, str):
        return Markup('<span class="v v-str">"{}"</span>').format(_text(value))
    return Markup('<span class="v">{}</span>').format(_text(value))


def _node(value: object, depth: int, budget: _Budget) -> Markup:
    if isinstance(value, Mapping):
        items: list[tuple[object, object]] = list(value.items())  # pyright: ignore[reportUnknownArgumentType,reportUnknownVariableType]
        label = f"{{{len(items)}}}"
        pairs = True
    elif isinstance(value, list | tuple):
        items = [(i, v) for i, v in enumerate(value)]  # pyright: ignore[reportUnknownVariableType,reportUnknownArgumentType]
        label = f"[{len(items)}]"
        pairs = False
    else:
        return _scalar(value)
    if not items:
        return Markup('<span class="v">{}</span>').format("{}" if pairs else "[]")
    if depth >= MAX_DEPTH:
        return Markup('<span class="trunc muted">… truncated</span>')
    rows: list[Markup] = []
    for key, child in items:
        if not budget.take():
            rows.append(_TRUNCATED)
            break
        k = Markup('<span class="k">{}</span>').format(_text(key))
        rows.append(Markup("<li>{}: {}</li>").format(k, _node(child, depth + 1, budget)))
    return Markup("<details{}><summary>{}</summary><ul>{}</ul></details>").format(
        Markup(" open") if depth < OPEN_DEPTH else Markup(""),
        label,
        Markup("").join(rows),
    )


def to_tree(obj: object, *, depth: int = 0) -> Markup:
    """Render ``obj`` as nested ``<details>``/``<ul>``. ``depth`` is the starting depth (it
    decides which levels start expanded). Never raises on odd or cyclic input."""
    return _node(obj, depth, _Budget())
