"""Reliable edits of the sketch being edited: split a segment, trim it to the
nearest intersections. Born from the TORUK rev2 work, where SolidWorks' raw
calls behaved erratically on offset splines.

SolidWorks facts these wrappers rest on (verified live on SW 2025, 04.11.2026):
- ISketchSegment.GetCurve() lives in SKETCH space, also on a face sketch: feed
  it sketch coordinates (a model-space point comes back as a wrong projection).
- ISketchManager.SplitOpenSegment splits ONLY at a point exactly on the curve
  (ICurve.GetClosestPointOn first; a couple of mm off and nothing happens), and
  it can answer None for a split that DID happen: the segment count is the
  truth, never the return value.
- ISketchManager.SketchTrim(swSketchTrimClosest = 0, x, y, z) with the segment
  preselected and an exact on-curve pick removes the picked portion up to the
  nearest intersections with the other sketch segments (power trim). It answers
  False even when it trims, so the result is read from the segments themselves.

Points are in mm. space='model' (default) takes a model-space [x, y, z] and
maps it onto the sketch; space='sketch' takes the sketch's own [u, v].
"""

import math

from .. import binding
from ..errors import SolidWorksError
from ..units import m_to_mm, mm_to_m

SW_SKETCH_TRIM_CLOSEST = 0  # swSketchTrimChoice_e, read from swconst.tlb of SW 2025
_JOIN_TOL_M = 2e-6   # offset piece ends this close count as joined
_TESS_TOL_M = 2e-4   # loop polygon sampling for the inside test


def active_sketch(session):
    """(model, ISketchManager, ISketch) of the sketch being edited; loud when none is."""
    model = session._require_model()
    sketch = binding.wrap(model.GetActiveSketch2(), session._mod.ISketch)
    if sketch is None:
        raise SolidWorksError("No sketch is being edited: open one first (InsertSketch/EditSketch, "
                              "with ui_unlocked() inside a script).")
    sk = binding.wrap(model.SketchManager, session._mod.ISketchManager)
    return model, sk, sketch


def _segments(session, sketch) -> list:
    return [binding.wrap(s, session._mod.ISketchSegment) for s in (sketch.GetSketchSegments() or ())]


def _seg_key(segment) -> tuple:
    return (int(segment.GetType()), tuple(segment.GetID()))


def snapshot(session, sketch) -> dict:
    """{segment key: length in m} of the active sketch, construction left out."""
    out = {}
    for seg in _segments(session, sketch):
        if bool(seg.ConstructionGeometry):
            continue
        out[_seg_key(seg)] = float(seg.GetLength())
    return out


def diff_lengths(before: dict, after: dict) -> dict:
    """What changed between two snapshots; pure, unit-tested.

    Keys may be reused by SolidWorks after a split (same id, new geometry), so a
    key present in both but with another length counts as changed."""
    removed = [k for k in before if k not in after]
    added = [k for k in after if k not in before]
    changed = [k for k in before if k in after and abs(before[k] - after[k]) > 1e-9]
    return {"removed": removed, "added": added, "changed": changed,
            "length_before_m": sum(before.values()), "length_after_m": sum(after.values())}


def resolve_segment(session, sketch, segment):
    """A segment of the sketch by index (int or '#3'), or the COM object itself."""
    if hasattr(segment, "GetID") or hasattr(segment, "_oleobj_"):
        return binding.wrap(segment, session._mod.ISketchSegment)
    segments = [s for s in _segments(session, sketch) if not bool(s.ConstructionGeometry)]
    key = str(segment).lstrip("#")
    try:
        index = int(key)
    except ValueError:
        raise SolidWorksError(f"segment must be an index (0..{len(segments) - 1}, also '#2') or the "
                              f"ISketchSegment itself (got {segment!r}).") from None
    if not 0 <= index < len(segments):
        raise SolidWorksError(f"No segment {index}: the sketch has {len(segments)} "
                              "(construction geometry not counted).")
    return segments[index]


def _sketch_uv_m(session, sketch, point_mm, space: str) -> tuple:
    """A point (mm) as the sketch's own (u, v) in m. space 'model' or 'sketch'."""
    values = [float(c) for c in point_mm]
    if space == "sketch":
        if len(values) != 2:
            raise SolidWorksError(f"space='sketch' takes [u, v] in mm (got {point_mm}).")
        return mm_to_m(values[0]), mm_to_m(values[1])
    if space != "model":
        raise SolidWorksError(f"space is 'model' or 'sketch' (got {space!r}).")
    if len(values) != 3:
        raise SolidWorksError(f"space='model' takes [x, y, z] in mm (got {point_mm}).")
    local = session._sketch_coords(sketch, *(mm_to_m(c) for c in values))
    return local[0], local[1]


def _on_curve(session, segment, u_m: float, v_m: float) -> tuple:
    """The exact point of the segment's curve nearest (u, v); sketch space, m."""
    curve = binding.wrap(segment.GetCurve(), session._mod.ICurve)
    if curve is None:
        raise SolidWorksError("The segment has no curve to work on (text?).")
    nearest = curve.GetClosestPointOn(u_m, v_m, 0.0)
    if not nearest:
        raise SolidWorksError("ICurve.GetClosestPointOn answered nothing for the pick point.")
    return float(nearest[0]), float(nearest[1]), m_to_mm(math.dist((u_m, v_m), (nearest[0], nearest[1])))


def _refuse_locked(session, segment, what: str) -> None:
    reason = locked_by(session, segment)
    if reason:
        raise SolidWorksError(
            f"SolidWorks will not {what} this segment: it is {reason}. Such a segment stays whole "
            "so its relation can drive it. For an offset contour use offset_face_loop (it closes "
            "the contour as a sketch region, no trimming); otherwise delete the relation first, "
            "which unlinks the segment from its dimension.")


def split_segment(session, segment, point_mm, space: str = "model") -> dict:
    """Split an open segment of the active sketch at the point of it nearest `point_mm`.

    SplitOpenSegment itself wants the point exactly on the curve and its return
    value lies, so the point is put on the curve first and the split is verified
    by the segment count. A closed circle cannot be split in one point; say so.
    """
    model, sk, sketch = active_sketch(session)
    seg = resolve_segment(session, sketch, segment)
    _refuse_locked(session, seg, "split")
    u, v, off_mm = _on_curve(session, seg, *_sketch_uv_m(session, sketch, point_mm, space))
    before = snapshot(session, sketch)
    model.ClearSelection2(True)
    sk.SplitOpenSegment(u, v, 0.0)  # the return value is None also on success
    after = snapshot(session, sketch)
    if len(after) != len(before) + 1:
        ends = "" if int(seg.GetType()) else " (SplitOpenSegment only splits OPEN segments; a circle needs two points)"
        raise SolidWorksError(
            f"SolidWorks did not split the segment at ({m_to_mm(u):.3f}, {m_to_mm(v):.3f}) mm (sketch "
            f"coordinates): the point may be at one of its ends, or the segment is closed{ends}. "
            f"The sketch still has {len(after)} segments.")
    change = diff_lengths(before, after)
    return {"ok": True, "split_at_sketch_mm": [round(m_to_mm(u), 4), round(m_to_mm(v), 4)],
            "pick_off_curve_mm": round(off_mm, 4),
            "segments_before": len(before), "segments_after": len(after),
            "piece_lengths_mm": sorted(round(m_to_mm(after[k]), 4) for k in (*change["added"], *change["changed"]))}


def trim_to_intersection(session, segment, pick_mm, space: str = "model") -> dict:
    """Remove the piece of `segment` nearest `pick_mm`, up to its nearest
    intersections with the other sketch segments (SolidWorks' power trim).

    The pick is put exactly on the curve and the segment preselected: without
    both, SketchTrim(closest) cuts at arbitrary spots or does nothing. The
    outcome is verified against the sketch, because SketchTrim answers False
    for trims that happened.
    """
    model, sk, sketch = active_sketch(session)
    seg = resolve_segment(session, sketch, segment)
    _refuse_locked(session, seg, "trim")
    target_key = _seg_key(seg)
    u, v, off_mm = _on_curve(session, seg, *_sketch_uv_m(session, sketch, pick_mm, space))
    before = snapshot(session, sketch)
    model.ClearSelection2(True)
    if not seg.Select4(False, None):
        raise SolidWorksError("Could not preselect the segment to trim.")
    sk.SketchTrim(SW_SKETCH_TRIM_CLOSEST, u, v, 0.0)  # the return value lies; verified below
    model.ClearSelection2(True)
    after = snapshot(session, sketch)
    change = diff_lengths(before, after)
    if not (change["removed"] or change["added"] or change["changed"]):
        raise SolidWorksError(
            f"SolidWorks did not trim anything at ({m_to_mm(u):.3f}, {m_to_mm(v):.3f}) mm (sketch "
            "coordinates). The picked portion may already end at intersections, or the pick lies "
            "on another segment: pass the segment the piece belongs to.")
    removed_mm = m_to_mm(change["length_before_m"] - change["length_after_m"])
    return {"ok": True, "pick_on_curve_sketch_mm": [round(m_to_mm(u), 4), round(m_to_mm(v), 4)],
            "pick_off_curve_mm": round(off_mm, 4), "removed_mm": round(removed_mm, 4),
            "segment_deleted": target_key in change["removed"] and len(after) < len(before),
            "segments_before": len(before), "segments_after": len(after)}


SW_CONSTRAINT_USEEDGE = 32       # swConstraintType_e: a converted edge (On Edge)
SW_CONSTRAINT_SKETCHOFFSET = 47  # swConstraintType_e: an Offset Entities result
_LOCKING = {SW_CONSTRAINT_USEEDGE: "converted from a model edge (On Edge)",
            SW_CONSTRAINT_SKETCHOFFSET: "an Offset Entities result (offset relation)"}


def locked_by(session, segment) -> str | None:
    """Why SolidWorks will neither split nor trim this segment, or None.

    Verified on SW 2025: a segment held by an offset relation or converted
    from an edge refuses SplitOpenSegment and SketchTrim silently. This is the
    'SplitOpenSegment returns None on offset splines' defect: it is
    SolidWorks' rule, not a marshalling problem."""
    try:
        relations = segment.GetRelations() or ()
    except Exception:  # noqa: BLE001 - older builds lack it; nothing to report then
        return None
    for raw in relations:
        try:
            kind = int(binding.wrap(raw, session._mod.ISketchRelation).GetRelationType())
        except Exception:  # noqa: BLE001
            continue
        if kind in _LOCKING:
            return _LOCKING[kind]
    return None


