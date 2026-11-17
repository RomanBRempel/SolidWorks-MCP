"""Picking faces and edges by what they are, not by a ray or an index (S-09).

'#12' means the twelfth face of the body as SolidWorks lists it now: any
rebuild that adds or removes a face renumbers the rest. A ray hits whatever
is in front. find_faces / find_edges describe the entity instead - its
surface, where it faces, what it is parallel to, its size, the feature that
made it, the body it belongs to - and say how many must match (`expect`).

Each match comes back with `ref`: a short name ('ref:' + 12 hex digits) of
SolidWorks' persistent reference (IModelDocExtension.GetPersistReference3). It
names the same face or edge after rebuilds and in the next session, and the
tools that take a face or an edge take it in place of '#N' / '+z' (see
`entity` and `install`).

The reference itself is 360-420 bytes per face or edge (measured on SW 2025
SP3: a box with a boss and a hole), 500-560 characters as text - thirty faces
would cost 16 KB of reply. So the reply carries the first 12 hex digits of its
SHA-1 and the bytes go to <home>/refs.jsonl, shared by every server process
and kept across restarts; the full base64 form is still accepted.
"""

import base64
import hashlib
import math
import os

from .. import binding
from ..errors import SolidWorksError
from ..session import SolidWorksSession
from . import bodies as bodies_mod
from . import fastpath, journal, render

REF_PREFIX = "ref:"
SURFACE_NAMES = {4001: "plane", 4002: "cylinder", 4003: "cone", 4004: "sphere", 4005: "torus",
                 4006: "spline", 4007: "blend", 4008: "offset", 4009: "extrusion", 4010: "revolution"}
CURVE_NAMES = {3001: "line", 3002: "circle", 3003: "ellipse", 3004: "intersection", 3005: "spline",
               3006: "spcurve", 3008: "constparam", 3009: "trimmed"}
PLANE_NORMALS = {"front": (0.0, 0.0, 1.0), "top": (0.0, 1.0, 0.0), "right": (1.0, 0.0, 0.0)}
AXES = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}
ALONG = 0.9995   # cos of ~1.8 degrees: "the same direction"
ACROSS = 0.03    # |cos| below this: "perpendicular"

_ORIGINAL = {}
_session = None


# --- pure parts (unit-tested) -------------------------------------------------------------------


SHORT_LEN = 12
NEWLINE = bytes([10])
STORE_MAX_BYTES = 8 * 1024 * 1024   # ~14 000 references; the older half goes when it is passed

_store = {}        # short name -> reference bytes, as far as this process has seen
_unsaved = []      # lines to append to the store file
_store_seen = 0    # bytes of the store file already read
_store_tail = b""  # the last bytes read before that offset: they tell a pruned file from a grown one
_on_disk = set()   # names known to have a line in the file as it is now
_pending = set()   # names waiting in _unsaved
TAIL_BYTES = 48


def short_of(ref: bytes) -> str:
    """The short name of a persistent reference; pure."""
    return hashlib.sha1(bytes(ref)).hexdigest()[:SHORT_LEN]


def _store_path() -> str:
    return os.path.join(journal.home(), "refs.jsonl")


def _read_store() -> None:
    """Take in what the store file gained since the last look (other processes append too).

    Another process may have pruned the file meanwhile (the older half dropped, the rest
    rewritten). A shorter file shows that; a pruned file that grew past the old offset
    again does not, so the bytes just before the offset are compared too. A pruned file
    is read again from the top, and what is known to be on disk starts over."""
    global _store_seen, _store_tail
    path = _store_path()
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    try:
        with open(path, "rb") as fh:
            if size < _store_seen:
                _store_seen = 0
            elif _store_seen:
                fh.seek(_store_seen - len(_store_tail))
                if fh.read(len(_store_tail)) != _store_tail:
                    _store_seen = 0
            if not _store_seen:
                _store_tail = b""
                _on_disk.clear()
            if size == _store_seen:
                return
            fh.seek(_store_seen)
            data = fh.read()
    except OSError:
        return
    end = data.rfind(NEWLINE) + 1  # a line still being written is left for the next look
    _store_seen += end
    _store_tail = (_store_tail + data[:end])[-TAIL_BYTES:]
    for line in data[:end].splitlines():
        name, _, body = line.partition(b" ")
        try:
            key = name.decode("ascii")
            _store[key] = base64.b64decode(body)
            _on_disk.add(key)
        except Exception:  # noqa: BLE001 - a damaged line names nothing
            continue


def flush() -> None:
    """Write the references handed out since the last flush to the store file."""
    global _store_seen, _store_tail
    if not _unsaved:
        return
    path = _store_path()
    lines, _unsaved[:] = list(_unsaved), []
    _pending.clear()
    try:
        if os.path.exists(path) and os.path.getsize(path) > STORE_MAX_BYTES:
            _read_store()
            with open(path, "rb") as fh:
                kept = fh.read().splitlines(keepends=True)
            kept = kept[len(kept) // 2:]
            with open(path + ".tmp", "wb") as fh:
                fh.writelines(kept)
            os.replace(path + ".tmp", path)
            _store_seen, _store_tail = 0, b""   # what is on disk is read again below
            _on_disk.clear()
        with open(path, "ab") as fh:
            fh.write("".join(lines).encode("ascii"))
        _read_store()
    except OSError:
        pass  # the references still resolve in this process


def to_token(ref: bytes) -> str:
    """'ref:<short name>' for a persistent reference; the bytes are remembered (see flush)."""
    ref = bytes(ref)
    name = short_of(ref)
    _store.setdefault(name, ref)
    if name not in _on_disk and name not in _pending:
        _read_store()   # another process may have written it; or its line was pruned: write it again
        if name not in _on_disk:
            _pending.add(name)
            _unsaved.append(f"{name} {base64.b64encode(ref).decode('ascii')}{NEWLINE.decode()}")
    return REF_PREFIX + name


def from_token(token) -> bytes | None:
    """The persistent reference behind 'ref:...', or None when the text is no such token."""
    text = str(token).strip()
    if text[:len(REF_PREFIX)].lower() != REF_PREFIX:
        return None
    body = text[len(REF_PREFIX):].strip()
    if body and len(body) <= 2 * SHORT_LEN and all(c in "0123456789abcdefABCDEF" for c in body):
        name = body.lower()
        if name not in _store:
            _read_store()
        if name not in _store:
            raise SolidWorksError(f"No reference '{REF_PREFIX}{name}' is known here: find_faces / find_edges give "
                                  "them (the store keeps the latest ~14 000). Find the entity again.")
        return _store[name]
    problem = SolidWorksError(f"'{text[:24]}...' is not a valid entity reference (find_faces / find_edges give "
                              "them).")
    if not body or any(not (c.isascii() and (c.isalnum() or c in "-_")) for c in body):
        raise problem   # b64decode would drop foreign characters silently and answer b''
    try:  # the reference itself, base64
        return base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except Exception:  # noqa: BLE001
        raise problem from None


def is_ref(token) -> bool:
    return isinstance(token, str) and token.strip()[:len(REF_PREFIX)].lower() == REF_PREFIX


def unit(vector) -> tuple:
    length = math.sqrt(sum(float(c) ** 2 for c in vector))
    if not length:
        raise SolidWorksError(f"A direction needs a non-zero vector (got {vector!r}).")
    return tuple(float(c) / length for c in vector)


def direction(value) -> tuple:
    """A unit vector from '+z' / '-x' / 'y' or [x, y, z]; pure."""
    if isinstance(value, str):
        key = value.strip().lower()
        sign = -1.0 if key.startswith("-") else 1.0
        axis = key.lstrip("+-")
        if axis not in AXES:
            raise SolidWorksError(f"A direction is '+x', '-y', 'z' or [x, y, z] (got {value!r}).")
        return tuple(sign * c for c in AXES[axis])
    return unit(value)


def parallel_spec(value) -> tuple:
    """('plane', normal) for 'front' / 'top' / 'right', ('line', direction) for an axis or
    vector; pure."""
    if isinstance(value, str) and value.strip().lower() in PLANE_NORMALS:
        return "plane", PLANE_NORMALS[value.strip().lower()]
    return "line", direction(value)


def count_ok(expect, found: int) -> str | None:
    """Why `found` matches do not meet `expect` (an exact count or [min, max]), or None; pure."""
    if expect is None:
        return None
    problem = SolidWorksError(f"expect is a count or [min, max] of counts (got {expect!r}).")
    if isinstance(expect, (list, tuple)) and len(expect) == 2:
        try:
            low, high = int(expect[0]), int(expect[1])
        except (TypeError, ValueError):
            raise problem from None
        return None if low <= found <= high else f"{low} to {high} were expected"
    try:
        want = int(expect)
    except (TypeError, ValueError):
        raise problem from None
    return None if found == want else f"{want} {'was' if want == 1 else 'were'} expected"


def in_box(box_m, within_mm) -> bool:
    """Does the entity's box (6 doubles, m) lie inside the two-corner box (mm)? pure."""
    low = [min(a, b) for a, b in zip(*within_mm)]
    high = [max(a, b) for a, b in zip(*within_mm)]
    return all(low[i] - 1e-4 <= box_m[i] * 1000.0 and box_m[i + 3] * 1000.0 <= high[i] + 1e-4 for i in range(3))


def in_range(value: float, bounds) -> bool:
    if bounds is None:
        return True
    if isinstance(bounds, (int, float)):
        return abs(value - float(bounds)) <= max(1e-3, 1e-4 * abs(float(bounds)))
    try:
        low, high = bounds
        low = None if low is None else float(low)
        high = None if high is None else float(high)
    except (TypeError, ValueError):
        raise SolidWorksError(f"A range is a number or [min, max] with None for an open end (got {bounds!r}).") from None
    return (low is None or value >= low - 1e-9) and (high is None or value <= high + 1e-9)


# --- references ---------------------------------------------------------------------------------


def _ext(session):
    return binding.wrap(session._require_model().Extension, session._mod.IModelDocExtension)


def ref_of(session, entity, ext=None) -> str:
    ext = ext or _ext(session)
    return to_token(bytes(ext.GetPersistReference3(entity)))


def entity(session, token, kind: str | None = None):
    """The face ('face'), edge ('edge') or vertex behind a 'ref:...' token, early-bound."""
    ref = from_token(token)
    if ref is None:
        raise SolidWorksError(f"'{token}' is not an entity reference.")
    mod = session._mod
    try:
        found = _ext(session).GetObjectByPersistReference3(ref)
    except Exception as exc:  # noqa: BLE001
        raise SolidWorksError(f"SolidWorks could not resolve the entity reference: {exc}") from None
    obj, state = (found[0], found[1]) if isinstance(found, tuple) else (found, 0)
    if obj is None:
        raise SolidWorksError(
            f"The referenced {kind or 'entity'} is gone (state {state}): the feature that made it was deleted, "
            "suppressed or rolled back, or the reference belongs to another document. Find it again.")
    cls = {"face": mod.IFace2, "edge": mod.IEdge, "vertex": mod.IVertex}.get(kind)
    if cls is None:
        return obj
    wrapped = binding.wrap(obj, cls)
    try:
        wrapped.GetArea() if kind == "face" else wrapped.GetCurve() if kind == "edge" else wrapped.GetPoint()
    except Exception:  # noqa: BLE001
        raise SolidWorksError(f"The reference does not name a {kind}.") from None
    return wrapped


# --- reading entities ---------------------------------------------------------------------------


def _bodies(session, body) -> list:
    every = bodies_mod._all_bodies(session)
    if body is None:
        return every
    names = bodies_mod.as_names(body)
    chosen = [b for b in every if b.Name in names]
    missing = [n for n in names if n not in {b.Name for b in chosen}]
    if missing:
        raise SolidWorksError(f"No body {missing} (there are: {', '.join(b.Name for b in every) or 'none'}).")
    return chosen


def _face_data(session, face) -> dict:
    mod = session._mod
    surface = binding.wrap(face.GetSurface(), mod.ISurface)
    kind = SURFACE_NAMES.get(int(surface.Identity()), "other") if surface is not None else "other"
    data = {"surface": kind, "box": tuple(face.GetBox())}
    if kind == "plane":
        data["normal"] = tuple(float(c) for c in face.Normal)
    elif kind == "cylinder":
        params = surface.CylinderParams  # origin xyz, axis xyz, radius (m)
        data["axis"] = unit(params[3:6])
        data["radius_mm"] = float(params[6]) * 1000.0
        data["axis_point_mm"] = [float(c) * 1000.0 for c in params[0:3]]
    return data


def _face_entry(session, face, data: dict, body_name: str, ext) -> dict:
    mod = session._mod
    box = data["box"]
    entry = {"ref": ref_of(session, face, ext), "surface": data["surface"], "body": body_name,
             "area_mm2": round(float(face.GetArea()) * 1e6, 4),
             "box_mm": [[round(c * 1000.0, 4) for c in box[0:3]], [round(c * 1000.0, 4) for c in box[3:6]]]}
    if "normal" in data:
        entry["normal"] = [round(c, 6) + 0.0 for c in data["normal"]]
    if "radius_mm" in data:
        entry.update(radius_mm=round(data["radius_mm"], 4), axis=[round(c, 6) + 0.0 for c in data["axis"]],
                     axis_point_mm=[round(c, 4) for c in data["axis_point_mm"]])
    try:
        owner = binding.wrap(face.GetFeature(), mod.IFeature)
        entry["feature"] = owner.Name if owner is not None else None
    except Exception:  # noqa: BLE001
        entry["feature"] = None
    return entry


def _feature_faces(session, name: str) -> set:
    """Persistent references of the faces a feature made."""
    feat = session._history_feature(name)
    ext = _ext(session)
    return {bytes(ext.GetPersistReference3(f)) for f in (render._raw(feat).GetFaces() or ())}


def _answer(kind: str, matches: list, expect, limit: int, scanned: int) -> dict:
    problem = count_ok(expect, len(matches))
    if problem:
        shown = [{k: m[k] for k in m if k != "ref"} for m in matches[:8]]
        raise SolidWorksError(f"{len(matches)} {kind}(s) match, {problem}. "
                              + (f"The matches: {shown}" if matches else "Loosen the filters."))
    limit = max(1, int(limit))
    flush()
    return {"ok": True, "count": len(matches), kind + "s": matches[:limit], "scanned": scanned,
            "truncated": len(matches) > limit}


def find_faces(session, surface: str | None = None, normal=None, parallel_to=None, within_mm=None, at_mm=None,
               area_mm2=None, radius_mm=None, feature: str | None = None, body=None, expect=None,
               limit: int = 30) -> dict:
    """Faces of the current part that fit a description (see the module doc)."""
    session._require_model()
    session._require_part()
    mod = session._mod
    want_normal = direction(normal) if normal is not None else None
    parallel = parallel_spec(parallel_to) if parallel_to is not None else None
    if surface is not None and str(surface).lower() not in set(SURFACE_NAMES.values()) | {"other"}:
        raise SolidWorksError(f"surface is one of {', '.join(sorted(set(SURFACE_NAMES.values())))} (got {surface!r}).")
    if within_mm is not None and (len(within_mm) != 2 or any(len(c) != 3 for c in within_mm)):
        raise SolidWorksError("within_mm needs two corners [[x1, y1, z1], [x2, y2, z2]].")
    owned = _feature_faces(session, feature) if feature is not None else None
    ext = _ext(session)
    first_body = None
    try:
        first_body = session._solid_body().Name
    except SolidWorksError:
        pass
    matches, scanned = [], 0
    for body_obj in _bodies(session, body):
        body_name = body_obj.Name
        for index, raw in enumerate(render._raw(body_obj).GetFaces() or ()):
            face = fastpath._bind(raw, mod.IFace2)
            scanned += 1
            if owned is not None and bytes(ext.GetPersistReference3(face)) not in owned:
                continue
            data = _face_data(session, face)
            if surface is not None and data["surface"] != str(surface).lower():
                continue
            if within_mm is not None and not in_box(data["box"], within_mm):
                continue
            if want_normal is not None:
                if "normal" not in data or sum(a * b for a, b in zip(data["normal"], want_normal)) < ALONG:
                    continue
            if parallel is not None:
                mode, vector = parallel
                if "normal" in data:
                    dot = abs(sum(a * b for a, b in zip(data["normal"], vector)))
                    if (mode == "plane" and dot < ALONG) or (mode == "line" and dot > ACROSS):
                        continue
                elif "axis" in data:
                    dot = abs(sum(a * b for a, b in zip(data["axis"], vector)))
                    if (mode == "line" and dot < ALONG) or (mode == "plane" and dot > ACROSS):
                        continue
                else:
                    continue
            if radius_mm is not None and not ("radius_mm" in data and in_range(data["radius_mm"], radius_mm)):
                continue
            if at_mm is not None:
                nearest = face.GetClosestPointOn(*(float(c) / 1000.0 for c in at_mm))
                if not nearest or math.dist([float(c) for c in at_mm], [c * 1000.0 for c in nearest[:3]]) > 0.01:
                    continue
            entry = _face_entry(session, face, data, body_name, ext)
            if area_mm2 is not None and not in_range(entry["area_mm2"], area_mm2):
                continue
            if body_name == first_body:
                entry["index"] = f"#{index}"
            matches.append(entry)
    return _answer("face", matches, expect, limit, scanned)


def _edge_entry(session, edge, ext, body_name: str) -> dict:
    mod = session._mod
    curve = binding.wrap(edge.GetCurve(), mod.ICurve)
    params = edge.GetCurveParams2()
    start = [float(c) * 1000.0 for c in params[0:3]]
    end = [float(c) * 1000.0 for c in params[3:6]]
    kind = CURVE_NAMES.get(int(curve.Identity()), "curve")
    closed = math.dist(start, end) < 1e-6
    entry = {"type": "arc" if kind == "circle" and not closed else kind, "body": body_name,
             "length_mm": round(float(curve.GetLength3(params[6], params[7])) * 1000.0, 4),
             "start_mm": [round(c, 4) for c in start], "end_mm": [round(c, 4) for c in end]}
    if kind == "line":
        entry["direction"] = [round(c, 6) + 0.0 for c in unit([b - a for a, b in zip(start, end)])]
    elif kind == "circle":
        cp = curve.CircleParams  # centre xyz, axis xyz, radius (m)
        entry.update(centre_mm=[round(float(c) * 1000.0, 4) for c in cp[0:3]],
                     axis=[round(float(c), 6) + 0.0 for c in cp[3:6]], radius_mm=round(float(cp[6]) * 1000.0, 4))
    entry["ref"] = ref_of(session, edge, ext)
    return entry


def find_edges(session, kind: str | None = None, parallel_to=None, length_mm=None, radius_mm=None, within_mm=None,
               at_mm=None, face=None, between=None, feature: str | None = None, body=None, expect=None,
               limit: int = 30) -> dict:
    """Edges of the current part that fit a description (see the module doc)."""
    session._require_model()
    session._require_part()
    mod = session._mod
    known = {"line", "circle", "arc", "ellipse", "spline", "curve"}
    if kind is not None and str(kind).lower() not in known:
        raise SolidWorksError(f"kind is one of {', '.join(sorted(known))} (got {kind!r}).")
    vector = direction(parallel_to) if parallel_to is not None else None
    if within_mm is not None and (len(within_mm) != 2 or any(len(c) != 3 for c in within_mm)):
        raise SolidWorksError("within_mm needs two corners [[x1, y1, z1], [x2, y2, z2]].")
    ext = _ext(session)

    def edges_of(face_token) -> set:
        from . import tools as rbr_tools
        face_obj = entity(session, face_token, "face") if is_ref(face_token) else \
            binding.wrap(rbr_tools._face_for(session, str(face_token)), mod.IFace2)
        return {bytes(ext.GetPersistReference3(e)) for e in (render._raw(face_obj).GetEdges() or ())}

    allowed = None
    if face is not None:
        allowed = edges_of(face)
    if between is not None:
        if not isinstance(between, (list, tuple)) or len(between) != 2:
            raise SolidWorksError("between takes two faces: the edge(s) they share.")
        shared = edges_of(between[0]) & edges_of(between[1])
        allowed = shared if allowed is None else allowed & shared
    if feature is not None:
        feat = session._history_feature(feature)
        made = set()
        for f in render._raw(feat).GetFaces() or ():
            made |= {bytes(ext.GetPersistReference3(e))
                     for e in (render._raw(fastpath._bind(f, mod.IFace2)).GetEdges() or ())}
        allowed = made if allowed is None else allowed & made
    first_body = None
    try:
        first_body = session._solid_body().Name
    except SolidWorksError:
        pass
    matches, scanned = [], 0
    for body_obj in _bodies(session, body):
        body_name = body_obj.Name
        for index, raw in enumerate(render._raw(body_obj).GetEdges() or ()):
            edge = fastpath._bind(raw, mod.IEdge)
            scanned += 1
            if allowed is not None and bytes(ext.GetPersistReference3(edge)) not in allowed:
                continue
            entry = _edge_entry(session, edge, ext, body_name)
            if kind is not None:
                want = str(kind).lower()
                if want == "curve":
                    if entry["type"] in ("line", "circle", "arc"):
                        continue
                elif entry["type"] != want:
                    continue
            if vector is not None:
                along = entry.get("direction") or entry.get("axis")
                if along is None or abs(sum(a * b for a, b in zip(along, vector))) < ALONG:
                    continue
            if length_mm is not None and not in_range(entry["length_mm"], length_mm):
                continue
            if radius_mm is not None and not ("radius_mm" in entry and in_range(entry["radius_mm"], radius_mm)):
                continue
            if within_mm is not None:
                low = [min(a, b) for a, b in zip(*within_mm)]
                high = [max(a, b) for a, b in zip(*within_mm)]
                if not all(low[i] - 1e-4 <= p[i] <= high[i] + 1e-4 for p in (entry["start_mm"], entry["end_mm"])
                           for i in range(3)):
                    continue
            if at_mm is not None:
                nearest = edge.GetClosestPointOn(*(float(c) / 1000.0 for c in at_mm))
                if not nearest or math.dist([float(c) for c in at_mm], [c * 1000.0 for c in nearest[:3]]) > 0.01:
                    continue
            if body_name == first_body:
                entry["index"] = index
            matches.append(entry)
    return _answer("edge", matches, expect, limit, scanned)


# --- the other tools take a reference where they take '#N' or '+z' ---------------------------------


class _Picked:
    """Stands in for upstream's face side ('outer' / 'inner') when the selector was a
    reference: the face itself, already found."""

    def __init__(self, face):
        self.face = face


def ref_tokens(selector) -> list | None:
    """The 'ref:...' tokens of an edge selector - one token, several in a string separated
    by commas or spaces, or a list of them - or None when it is something else; pure."""
    if isinstance(selector, (list, tuple)):
        tokens = [str(t).strip() for t in selector]
    elif isinstance(selector, str):
        tokens = selector.replace(",", " ").split()
    else:
        return None
    return tokens if tokens and all(is_ref(t) for t in tokens) else None


def _face_index(selector):
    """Upstream's '#5' -> 5, also for 'ref:...': the face's index in the first solid body."""
    if not is_ref(selector) or _session is None:
        return _ORIGINAL["_face_index"](selector)
    session = _session
    face = entity(session, selector, "face")
    ext = _ext(session)
    wanted = bytes(ext.GetPersistReference3(face))
    for index, raw in enumerate(render._raw(session._solid_body()).GetFaces() or ()):
        if bytes(ext.GetPersistReference3(raw)) == wanted:
            return index
    raise SolidWorksError("The referenced face is not on the part's first solid body: this tool reaches only "
                          "that body's faces.")


def _parse_edge_indices(selector):
    """Upstream's '2,5' -> [2, 5], also for 'ref:...' tokens (one, several, or a list of them)."""
    tokens = ref_tokens(selector) if _session is not None else None
    if tokens is None:
        return _ORIGINAL["_parse_edge_indices"](selector)
    session = _session
    ext = _ext(session)
    wanted = [bytes(ext.GetPersistReference3(entity(session, t, "edge"))) for t in tokens]
    index_of = {bytes(ext.GetPersistReference3(raw)): i
                for i, raw in enumerate(render._raw(session._solid_body()).GetEdges() or ())}
    missing = [t for t, w in zip(tokens, wanted) if w not in index_of]
    if missing:
        raise SolidWorksError(f"{len(missing)} referenced edge(s) are not on the part's first solid body: "
                              f"{', '.join(missing)}.")
    return [index_of[w] for w in wanted]


def _parse_face_selector(self, token, default_side="outer"):
    """Upstream's '+z' / '+z:inner' -> (normal, side), also for 'ref:...': the planar face's
    own normal, and the face itself in place of the side."""
    if not is_ref(token):
        return _ORIGINAL["_parse_face_selector"](self, token, default_side)
    face = entity(self, token, "face")
    surface = binding.wrap(face.GetSurface(), self._mod.ISurface)
    if surface is None or not surface.IsPlane():
        raise SolidWorksError("The referenced face is not planar: this tool sketches on (or opens) a planar face.")
    return tuple(float(c) for c in face.Normal), _Picked(face)


def _select_picked(self, picked, label):
    self._model.ClearSelection2(True)
    if not binding.wrap(picked.face, self._mod.IEntity).Select4(False, None):
        raise SolidWorksError(f"Could not select the face {label}.")
    return picked.face


def _select_face_through(self, body, normal, side, point_mm, label):
    if isinstance(side, _Picked):
        return _select_picked(self, side, label)
    return _ORIGINAL["_select_face_through"](self, body, normal, side, point_mm, label)


def _select_planar_face(self, body, normal, label, side="outer"):
    if isinstance(side, _Picked):
        return _select_picked(self, side, label)
    return _ORIGINAL["_select_planar_face"](self, body, normal, label, side)


_STATIC = {"_face_index": _face_index, "_parse_edge_indices": _parse_edge_indices}
_METHODS = {"_parse_face_selector": _parse_face_selector, "_select_face_through": _select_face_through,
            "_select_planar_face": _select_planar_face}


def install(session) -> None:
    """Teach upstream's selectors the references (once; `session` is the server's one session)."""
    global _session
    _session = session
    if _ORIGINAL:
        return
    for name, fn in {**_STATIC, **_METHODS}.items():
        _ORIGINAL[name] = getattr(SolidWorksSession, name)
        fn.__wrapped__ = _ORIGINAL[name]
        setattr(SolidWorksSession, name, staticmethod(fn) if name in _STATIC else fn)


def uninstall() -> None:
    global _session
    for name, fn in _ORIGINAL.items():
        setattr(SolidWorksSession, name, staticmethod(fn) if name in _STATIC else fn)
    _ORIGINAL.clear()
    _session = None
