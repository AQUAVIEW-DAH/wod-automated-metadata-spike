"""Place a mooring's instruments at depth from the instrument table in its description.

The table lists serial numbers with a stated depth or a position along the line from a
reference point. A position becomes a depth only where a pressure-recording instrument on
the same line fixes the reference point; anything that cannot be placed says why.
"""
import csv
import io
import re
import statistics
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# The header cells an instrument table is read by (lower case, a unit in brackets removed).
_SERIAL = re.compile(r"^(?:serial(?:\s*(?:no\.?|num(?:ber)?|#))?|s\s*/\s*n|sn|ser\.?\s*no\.?)$")
_DEPTH = re.compile(r"^(?:(?:nominal|instrument|target|deployment|deployed|sensor|planned|"
                    r"design)\s+)?depth$|^z$")
_POSITION = re.compile(r"^(?:pos(?:ition)?|height(?:\s+above\s+\w+)?|hab|mab|"
                       r"(?:m(?:etres|eters)?\s+)?above\s+(?:bottom|seabed|sea\s*floor|anchor)|"
                       r"distance(?:\s+(?:above|below|from|along)\b.*)?)$")
_REF = re.compile(r"^ref(?:erence)?\.?(?:\s*(?:pt\.?|point|no\.?))?$")
_TYPE = re.compile(r"^(?:type|instrument|model|make|sensor|description)\b")
_UP = re.compile(r"\b(?:above|height|hab|mab)\b")
_DOWN = re.compile(r"\bbelow\b|\bfrom\s+(?:the\s+)?(?:top|float|surface|buoy)\b")
# What a reference point is says which way positions from it run: up from the bottom of a
# line or the anchor, down from its top or the float.
_REF_LOW = re.compile(r"\b(?:bottom|anchor|sea\s*bed|sea\s*floor|release|base)\b", re.I)
_REF_HIGH = re.compile(r"\b(?:top|float|buoy|surface)\b", re.I)
_UNIT = re.compile(r"[\(\[]\s*([^\)\]]*)\s*[\)\]]")
_FEET = {"ft", "feet", "foot"}
_SEPARATOR = re.compile(r"^\s*\|?[\s\-+:=|]*-[\s\-+:=|]*\|?\s*$")
_TEXT_SUFFIXES = {".txt", ".md", ".org", ".rst", ".csv", ".tsv", ".text", ""}
# A depth in a file name: `T_45m.nc`.
_NAME_DEPTH = re.compile(r"(?:^|[_\-. ])(\d{1,5}(?:\.\d+)?)\s?m(?=$|[_\-. ])", re.I)
_FILE_SERIAL = re.compile(r"(?:^|[_\-. ])(?:sn|s/n|serial)[_\-. ]?0*(\d+)", re.I)


def serial_key(text: Any) -> Optional[str]:
    """A serial number as compared: its last run of two or more digits, without leading
    zeros ('07819', 'SN07819' and '37SM56061-7819' are one instrument)."""
    runs = re.findall(r"\d{2,}", str(text or ""))
    return str(int(runs[-1])) if runs else None


def file_serial(name: str) -> Optional[str]:
    """The serial number a file name carries (`RBRsolo_SN100020.nc`), or None."""
    m = _FILE_SERIAL.search(Path(name).stem)
    return str(int(m.group(1))) if m else None


def depth_from_name(name: str) -> Optional[float]:
    """The single depth a file name states (`T_45m.nc` -> 45.0), or None."""
    found = {float(x) for x in _NAME_DEPTH.findall(Path(name).stem)}
    return found.pop() if len(found) == 1 and 0 <= next(iter(found)) <= 11000 else None


# ----------------------------------------------------------------------- reading
def _cells_of(line: str, style: str) -> List[str]:
    if style == "pipe":
        return [c.strip() for c in line.strip().strip("|").split("|")]
    return [c.strip() for c in next(csv.reader([line], delimiter=style))]


def _role(cell: str) -> Tuple[Optional[str], Optional[str], float]:
    """(role, direction hint, metres per unit) of a header cell."""
    unit_match = _UNIT.search(cell)
    unit = unit_match.group(1).strip().lower() if unit_match else ""
    name = re.sub(r"\s+", " ", _UNIT.sub("", cell)).strip().lower().rstrip(":")
    scale = 0.3048 if unit in _FEET else 1.0 if unit in ("", "m", "metres", "meters") else 0.0
    if _SERIAL.match(name) or name.startswith("serial"):
        return "serial", None, 1.0
    if _REF.match(name):
        return "ref", None, 1.0
    if _DEPTH.match(name):
        return ("depth" if scale else None), None, scale
    if _POSITION.match(name):
        hint = "up" if _UP.search(name) else "down" if _DOWN.search(name) else None
        return ("position" if scale else None), hint, scale
    if _TYPE.match(name):
        return "type", None, 1.0
    return None, None, 1.0


def _number(cell: str) -> Optional[float]:
    m = re.fullmatch(r"\s*([-+]?\d+(?:\.\d+)?)\s*(?:m)?\s*", cell or "")
    return float(m.group(1)) if m else None


def _tables(text: str) -> List[Dict[str, Any]]:
    """Every table in a text: pipe tables (org, markdown) and delimited ones, each as
    `{"rows": [[cells]], "blocks": [block number per row], "line": first line}`. A rule
    line inside a pipe table starts a new block."""
    lines = text.splitlines()
    out: List[Dict[str, Any]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("|") and line.strip().endswith("|") and \
                line.strip() != "|":
            table = {"rows": [], "blocks": [], "line": i + 1, "style": "pipe"}
            block = 0
            while i < len(lines) and lines[i].strip().startswith("|"):
                if _SEPARATOR.match(lines[i]):
                    if table["rows"]:
                        block += 1
                else:
                    table["rows"].append(_cells_of(lines[i], "pipe"))
                    table["blocks"].append(block)
                i += 1
            out.append(table)
            continue
        i += 1
    if out:
        return out
    # A delimited table: the first line that reads as a header, and the rows after it.
    for delimiter in ("\t", ",", ";"):
        rows = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter)]
        for start, row in enumerate(rows):
            roles = [_role(c)[0] for c in row]
            if "serial" in roles and ("depth" in roles or "position" in roles):
                body = [r for r in rows[start:] if len(r) >= 2]
                out.append({"rows": [[c.strip() for c in r] for r in body],
                            "blocks": [0] * len(body), "line": start + 1, "style": delimiter})
                break
        if out:
            break
    return out


def read_layout(path, name: Optional[str] = None) -> Dict[str, Any]:
    """The instrument table of a mooring description: `{"file", "rows": [{"row", "block",
    "type", "serial", "depth", "position", "ref"}], "references": {ref: description},
    "direction" (a header's "up"/"down", or None), "blocks", "error"}`. Depths and
    positions are in metres; a cell left empty is None."""
    p = Path(path)
    out: Dict[str, Any] = {"file": name or p.name, "rows": [], "references": {},
                           "direction": None, "blocks": 0, "error": None}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    for table in _tables(text):
        header, body, blocks = table["rows"][0], table["rows"][1:], table["blocks"][1:]
        roles = [_role(c) for c in header]
        kinds = [r[0] for r in roles]
        if kinds.count("ref") == 1 and len(header) == 2 and not ("serial" in kinds):
            # `Reference Point | Description`: what each reference point is.
            for row in body:
                if len(row) >= 2 and row[0]:
                    out["references"][row[0].strip()] = row[1].strip()
            continue
        if "serial" not in kinds or not ({"depth", "position"} & set(kinds)) or out["rows"]:
            continue
        col = {k: kinds.index(k) for k in ("serial", "depth", "position", "ref", "type")
               if k in kinds}
        if "position" in col:
            out["direction"] = roles[col["position"]][1]
        first_block = blocks[0] if blocks else 0
        for n, (row, block) in enumerate(zip(body, blocks)):
            cell = lambda k: row[col[k]] if k in col and col[k] < len(row) else ""
            serial = serial_key(cell("serial"))
            if serial is None:
                continue
            scale_d = roles[col["depth"]][2] if "depth" in col else 1.0
            scale_p = roles[col["position"]][2] if "position" in col else 1.0
            depth, position = _number(cell("depth")), _number(cell("position"))
            out["rows"].append({
                "row": n, "block": block - first_block, "type": cell("type") or None,
                "serial": serial,
                "depth": depth * scale_d if depth is not None else None,
                "position": position * scale_p if position is not None else None,
                "ref": cell("ref").strip() or None})
        out["blocks"] = len({r["block"] for r in out["rows"]})
    return out


def find_layout(candidates: Sequence[Tuple[str, Any]], members: Sequence[Dict[str, Any]]
                ) -> Optional[Dict[str, Any]]:
    """Of `(name, path)` text files, the instrument table naming the most of `members` (each
    `{"serial"}`), if it names at least two (one, for a single-instrument mooring)."""
    serials = {m.get("serial") for m in members if m.get("serial")}
    best, score = None, 0
    for name, path in candidates:
        if Path(name).suffix.lower() not in _TEXT_SUFFIXES:
            continue
        layout = read_layout(path, name)
        if layout["error"] or not layout["rows"]:
            continue
        named = len(serials & {r["serial"] for r in layout["rows"]})
        if named > score:
            best, score = layout, named
    return best if best is not None and score >= min(2, len(serials)) else None


# ----------------------------------------------------------------------- placing
def _tolerance(depth: float) -> float:
    """How far apart two placements of one point may be and still agree."""
    return max(5.0, 0.05 * abs(depth))


def _order(rows: List[Dict[str, Any]], known: Dict[int, float]) -> Optional[str]:
    """Whether the table lists the mooring top down ("down") or bottom up ("up"), by the
    depths of the instruments it lists that know theirs."""
    seq = [known[r["row"]] for r in rows if r["row"] in known]
    pairs = [(a, b) for i, a in enumerate(seq) for b in seq[i + 1:] if a != b]
    down = sum(1 for a, b in pairs if b > a)
    up = len(pairs) - down
    if down and down >= 3 * up:
        return "down"
    if up and up >= 3 * down:
        return "up"
    return None


def _effective_refs(rows: List[Dict[str, Any]]) -> Dict[int, Optional[str]]:
    """Each row's reference point. A blank one is the one reference the rest of its block
    names; failing that, the nearest named above it ('as above'); a table with no
    reference column measures every position from one point."""
    if not any(r["ref"] for r in rows):
        return {r["row"]: "" for r in rows}
    out: Dict[int, Optional[str]] = {}
    last = None
    for r in rows:
        if r["ref"]:
            out[r["row"]] = last = r["ref"]
            continue
        block = {x["ref"] for x in rows if x["block"] == r["block"] and x["ref"]}
        out[r["row"]] = block.pop() if len(block) == 1 else last
    return out


def place_instruments(layout: Dict[str, Any], members: Sequence[Dict[str, Any]]
                      ) -> Dict[str, Any]:
    """Depths for the members the layout places. Each member is `{"name", "serial",
    "depth", "by_pressure", "label"}`: `depth` its known depth (by its pressure, or as its
    file states) or None, and `label` how the sentences name it (else `name`).

    Returns `{"placed": {name: {"depth", "how", "detail", "short"}}, "unplaced": {name:
    why}, "problems": [...]}`. `how` is "stated", "position" or "beside"; `detail` says
    where it came from in a sentence, `short` in a few words."""
    source = layout["file"]
    rows = layout["rows"]
    count: Dict[str, int] = {}
    for r in rows:
        count[r["serial"]] = count.get(r["serial"], 0) + 1
    by_serial = {m["serial"]: m for m in members if m.get("serial")}
    member_of = {r["row"]: by_serial[r["serial"]] for r in rows
                 if r["serial"] in by_serial and count[r["serial"]] == 1}
    known = {row: float(m["depth"]) for row, m in member_of.items()
             if m.get("depth") is not None}
    pressure = {row for row, m in member_of.items() if m.get("by_pressure")}
    placed: Dict[str, Dict[str, Any]] = {}
    unplaced: Dict[str, str] = {}
    problems: List[str] = []
    for serial, n in count.items():
        if n > 1 and serial in by_serial:
            unplaced[by_serial[serial]["name"]] = f"{source} lists serial {serial} {n} times"
    wanted = {row: m for row, m in member_of.items() if m.get("depth") is None}
    if not wanted:
        return {"placed": placed, "unplaced": unplaced, "problems": problems}
    order = _order(rows, known)
    refs = _effective_refs(rows)
    describe = lambda ref: (f"reference point {ref} ({layout['references'][ref]})"
                            if ref in layout["references"] else
                            f"reference point {ref}" if ref else "its reference point")
    name_of = lambda row: member_of[row].get("label") or member_of[row]["name"]

    # 1. A depth the table states.
    for row, m in list(wanted.items()):
        r = rows[[x["row"] for x in rows].index(row)]
        if r["depth"] is not None:
            placed[m["name"]] = {"depth": round(r["depth"], 2), "how": "stated",
                                 "detail": f"{source} states {r['depth']:g} m",
                                 "short": f"as {source} states"}
            del wanted[row]

    # 2. A position along the line, from a reference point the pressure records fix.
    for ref in sorted({refs[r["row"]] for r in rows if r["position"] is not None
                       and refs[r["row"]] is not None}, key=str):
        on = [r for r in rows if refs[r["row"]] == ref]
        positioned = [r for r in on if r["position"] is not None]
        todo = [r for r in positioned if r["row"] in wanted]
        if not todo:
            continue
        anchors = [r for r in positioned if r["row"] in known]
        direction = layout["direction"]
        what = layout["references"].get(ref or "", "")
        if direction is None and bool(_REF_LOW.search(what)) != bool(_REF_HIGH.search(what)):
            direction = "up" if _REF_LOW.search(what) else "down"
        if direction is None and len({a["position"] for a in anchors}) >= 2:
            a, b = sorted(anchors, key=lambda x: x["position"])[::len(anchors) - 1]
            direction = "up" if known[b["row"]] < known[a["row"]] else "down"
        if direction is None and order is not None:
            steps = [b["position"] - a["position"] for a, b in zip(positioned, positioned[1:])]
            falling = all(s < 0 for s in steps) and steps
            rising = all(s > 0 for s in steps) and steps
            if falling or rising:
                direction = ("up" if falling else "down") if order == "down" else \
                    ("down" if falling else "up")
        why = None
        if not anchors:
            why = (f"{source} gives its position from {describe(ref)}, and no instrument "
                   f"with a position from that point records pressure to fix its depth")
        elif direction is None:
            why = (f"{source} gives its position from {describe(ref)}, but not whether "
                   f"positions run up or down the line")
        if why:
            for r in todo:
                unplaced[member_of[r["row"]]["name"]] = why
            continue
        sign = 1.0 if direction == "up" else -1.0      # depth = reference - sign * position
        estimates = [known[a["row"]] + sign * a["position"] for a in anchors]
        ref_depth = statistics.median(estimates)
        spread = max(abs(e - ref_depth) for e in estimates)
        disagree = None
        if spread > _tolerance(ref_depth):
            disagree = (f"the pressure records of the instruments on that line put "
                        f"{describe(ref)} {spread:.1f} m apart")
        elif ref_depth < 0:
            disagree = (f"the pressure of {' and '.join(name_of(a['row']) for a in anchors)} "
                        f"would put {describe(ref)} above the surface")
        # Instruments that record pressure and have no position of their own, listed
        # between two positions on this line, bound where the reference point can be.
        low, high, bounds = float("-inf"), float("inf"), []
        for r in on:
            if r["row"] not in known or r["position"] is not None:
                continue
            above = [x for x in positioned if x["row"] < r["row"]]
            below = [x for x in positioned if x["row"] > r["row"]]
            if not above or not below:
                continue
            p1, p2 = above[-1]["position"], below[0]["position"]
            lo, hi = sorted((known[r["row"]] + sign * p1, known[r["row"]] + sign * p2))
            low, high = max(low, lo), min(high, hi)
            bounds.append(name_of(r["row"]))
        moved = 0.0
        if bounds and disagree is None:
            if low > high + _tolerance(ref_depth):
                disagree = (f"{', '.join(bounds)} cannot all lie between the positions "
                            f"{source} lists them between")
            else:
                clipped = min(max(ref_depth, low), high) if low <= high else (low + high) / 2
                moved, ref_depth = abs(clipped - ref_depth), clipped
                if moved > _tolerance(ref_depth):
                    disagree = (f"{', '.join(bounds)}, by its pressure, lies {moved:.1f} m "
                                f"from where {source} lists it")
        if disagree:
            problems.append(f"{describe(ref)}: {disagree}")
            for r in todo:
                unplaced[member_of[r["row"]]["name"]] = (f"{source} gives its position from "
                                                         f"{describe(ref)}, but {disagree}")
            continue
        fixed_by = " and ".join(name_of(a["row"]) for a in anchors)
        for r in todo:
            depth = ref_depth - sign * r["position"]
            if depth < 0:
                unplaced[member_of[r["row"]]["name"]] = (f"its position in {source} would put "
                                                         f"it {-depth:.1f} m above the surface")
                continue
            placed[member_of[r["row"]]["name"]] = {
                "depth": round(depth, 1), "how": "position",
                "detail": (f"{r['position']:g} m {'up' if sign > 0 else 'down'} the line "
                           f"from {describe(ref)} in {source}; the pressure of {fixed_by} "
                           f"puts that point at {ref_depth:.1f} m"
                           + (f" (moved {moved:.1f} m to agree with {', '.join(bounds)})"
                              if moved >= 0.05 else "")),
                "short": (f"{r['position']:g} m {'up' if sign > 0 else 'down'} the line from "
                          f"{f'point {ref}' if ref else 'its reference point'} "
                          f"({ref_depth:.1f} m by {fixed_by})")}
            del wanted[r["row"]]

    # 3. Listed beside one pressure instrument, with no position of its own: on its frame.
    if layout["blocks"] >= 2:
        for row, m in list(wanted.items()):
            r = rows[[x["row"] for x in rows].index(row)]
            if r["position"] is not None:
                continue
            block = [x for x in rows if x["block"] == r["block"]]
            beside = [x for x in block if x["row"] in pressure]
            others = [x for x in block if x["position"] is not None and x["row"] not in pressure]
            if len(beside) == 1 and not others:
                anchor = beside[0]["row"]
                placed[m["name"]] = {
                    "depth": round(known[anchor], 1), "how": "beside",
                    "detail": f"{source} lists it with {name_of(anchor)}, with no position "
                              f"of its own: at that instrument's depth by its pressure, "
                              f"{known[anchor]:.1f} m",
                    "short": f"listed with {name_of(anchor)}, no position of its own"}
                del wanted[row]

    # Each placement must sit between the instruments listed above and below it that know
    # their depth; a table read wrong puts one out of order.
    if order is not None:
        for name, p in list(placed.items()):
            row = next(r for r, m in member_of.items() if m["name"] == name)
            before = [known[x] for x in sorted(known) if x < row]
            after = [known[x] for x in sorted(known) if x > row]
            before, after = (before[-1] if before else None), (after[0] if after else None)
            shallower, deeper = (before, after) if order == "down" else (after, before)
            tol = _tolerance(p["depth"])
            if (shallower is not None and p["depth"] < shallower - tol) or \
                    (deeper is not None and p["depth"] > deeper + tol):
                unplaced[name] = (f"placed at {p['depth']:.1f} m by {source}, which is out of "
                                  f"order with the instruments listed around it")
                problems.append(f"{name_of(row)}: {unplaced[name]}")
                del placed[name]
    for row, m in wanted.items():
        unplaced.setdefault(m["name"], f"{source} gives it no depth or position")
    return {"placed": placed, "unplaced": unplaced, "problems": problems}
