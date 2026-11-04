"""Extrude and cut a sketch - or ONE region of it - with an explicit direction (B4, B5).

FeatureExtrusion3 / FeatureCut4 take a Dir flag relative to the sketch normal,
and their defaults differ: a boss runs along the normal, a cut against it
(verified upstream). Scripts guessed Dir/Flip and checked the volume. Here the
caller says where the material goes - toward = [x, y, z] in model space - and
the flag follows from its sign against the sketch's normal; the volume is then
checked once, as a guard, never to search.

A region (one closed area of a sketch whose curves cross) is used when it
ALONE is selected through ISelectData with Mark = 4 (verified on SW 2025: a
cut of the lens two crossing offset splines enclose removed exactly that
area x depth). Pick the region by a point inside it (model space).
"""

import math

from .. import binding
from ..constants import (SW_END_COND_BLIND, SW_END_COND_THROUGH_ALL, SW_END_COND_UP_TO_SURFACE,
                         SW_START_SKETCH_PLANE)
from ..errors import SolidWorksError
from ..units import m_to_mm, mm_to_m
from . import offset_loop

MARK_PROFILE = offset_loop.MARK_PROFILE
MARK_END_FACE = 1


def sketch_frame(session, sketch_feature) -> dict:
    """origin_mm and unit normal of a sketch in model space."""
    sketch = binding.wrap(sketch_feature.GetSpecificFeature2(), session._mod.ISketch)
    to_model = binding.wrap(binding.wrap(sketch.ModelToSketchTransform, session._mod.IMathTransform).Inverse(),
                            session._mod.IMathTransform)
    origin = session._to_model_mm(to_model, 0.0, 0.0, 0.0)
    tip = session._to_model_mm(to_model, 0.0, 0.0, 0.001)
    normal = [b - a for a, b in zip(origin, tip)]
    length = math.sqrt(sum(c * c for c in normal)) or 1.0
    return {"sketch": sketch, "origin_mm": origin, "normal": [c / length for c in normal]}


def reverse_flag(toward, normal, cut: bool) -> bool:
    """The Dir flag that sends the feature `toward`; pure, unit-tested.

    A boss goes along the sketch normal unless reversed; a cut goes against it
    unless reversed. toward perpendicular to the normal is refused."""
    if toward is None or len(toward) != 3:
        raise SolidWorksError(f"toward must be a direction [x, y, z] in model space (got {toward}).")
    dot = sum(float(a) * float(b) for a, b in zip(toward, normal))
    length = math.sqrt(sum(float(c) ** 2 for c in toward))
    if length == 0 or abs(dot) < 1e-6 * length:
        raise SolidWorksError(f"toward {list(toward)} lies in the sketch plane (normal {[round(c, 4) for c in normal]}): "
                              "give a direction off the plane, toward where the material goes.")
    along_normal = dot > 0
    return along_normal if cut else not along_normal


def end_condition(depth_mm, through_all: bool, up_to) -> tuple:
    """(T1, D1 in m) for the end condition; pure, unit-tested. Exactly one given."""
    given = sum(x is not None and x is not False for x in (depth_mm, through_all or None, up_to))
    if given != 1:
        raise SolidWorksError("Give exactly one of depth_mm, through_all=True or up_to (a face).")
    if depth_mm is not None:
        if depth_mm <= 0:
            raise SolidWorksError(f"depth_mm must be > 0 (got {depth_mm}).")
        return SW_END_COND_BLIND, mm_to_m(depth_mm)
    if through_all:
        return SW_END_COND_THROUGH_ALL, 0.0
    return SW_END_COND_UP_TO_SURFACE, 0.0


def _face_of(session, up_to):
    """The face for up_to: '#index' / '+x' style selectors, or [x, y, z] mm on it."""
    from . import tools as rbr_tools
    if isinstance(up_to, (list, tuple)):
        return session._part_face_at([float(c) for c in up_to])
    text = str(up_to).strip()
    if text.startswith("@"):
        return session._part_face_at([float(c) for c in text[1:].split(",")])
    return rbr_tools._face_for(session, text)


def _volume_mm3(session) -> float:
    return float(session.get_mass_properties()["mass_properties"].get("volume_mm3") or 0.0)


def _select_profile(session, model, feature, frame, region_point_mm):
    """Select the whole sketch, or only the region around region_point_mm."""
    sm = binding.wrap(model.SelectionManager, session._mod.ISelectionMgr)
    model.ClearSelection2(True)
    if region_point_mm is None:
        if not feature.Select2(False, 0):
            raise SolidWorksError(f"Could not select the sketch '{feature.Name}'.")
        return None
    local = session._sketch_coords(frame["sketch"], *(mm_to_m(float(c)) for c in region_point_mm))
    if abs(m_to_mm(local[2])) > 0.05:
        raise SolidWorksError(f"region_point_mm {list(region_point_mm)} is {m_to_mm(local[2]):.3f} mm off the "
                              f"plane of '{feature.Name}': give a point in the sketch.")
    entry = offset_loop.region_at(session, frame["sketch"], (local[0], local[1]))
    if entry is None:
        raise SolidWorksError(f"No closed region of '{feature.Name}' contains {list(region_point_mm)} "
                              "(offset_face_loop returns a region_point_mm for its contour).")
    data = binding.wrap(sm.CreateSelectData(), session._mod.ISelectData)
    data.Mark = MARK_PROFILE
    if not entry["region"].Select2(False, data):
        raise SolidWorksError("Could not select the sketch region.")
    return entry


def _make(session, sketch: str, cut: bool, toward, depth_mm, through_all, up_to,
          merge: bool, region_point_mm, name: str | None) -> dict:
    model = session._require_model()
    session._require_part()
    feature = session._sketch_by_name(sketch)
    frame = sketch_frame(session, feature)
    reverse = reverse_flag(toward, frame["normal"], cut)
    t1, d1 = end_condition(depth_mm, through_all, up_to)
    face = _face_of(session, up_to) if up_to is not None else None
    bodies_before = {b.Name for b in session._part_bodies()}
    volume_before = _volume_mm3(session)
    region = _select_profile(session, model, feature, frame, region_point_mm)
    if face is not None:
        data = binding.wrap(binding.wrap(model.SelectionManager, session._mod.ISelectionMgr).CreateSelectData(),
                            session._mod.ISelectData)
        data.Mark = MARK_END_FACE
        if not binding.wrap(face, session._mod.IEntity).Select4(True, data):
            raise SolidWorksError("Could not select the face to run up to.")
    fm = binding.wrap(model.FeatureManager, session._mod.IFeatureManager)
    if cut:
        made = fm.FeatureCut4(True, False, reverse, t1, 0, d1, 0.0,
                              False, False, False, False, 0.0, 0.0,
                              False, False, False, False, False, True, True, False, False, False,
                              SW_START_SKETCH_PLANE, 0.0, False, False)
    else:
        made = fm.FeatureExtrusion3(True, False, reverse, t1, 0, d1, 0.0,
                                    False, False, False, False, 0.0, 0.0,
                                    False, False, False, False, bool(merge), True, True,
                                    SW_START_SKETCH_PLANE, 0.0, False)
    model.ClearSelection2(True)
    kind = "cut" if cut else "extrusion"
    if made is None:
        raise SolidWorksError(f"SolidWorks refused the {kind} of '{sketch}' toward {list(toward)}: "
                              "is the profile closed (or region_point_mm inside a closed region), and does "
                              "the end condition meet the part?")
    made = binding.wrap(made, session._mod.IFeature)
    result = session._finish_feature(made, name or made.Name, sketch=sketch,
                                     direction="along the sketch normal" if (reverse == cut) else
                                     "against the sketch normal")
    volume_after = float((result.get("mass_properties") or {}).get("volume_mm3") or 0.0)
    new_bodies = [b.Name for b in session._part_bodies() if b.Name not in bodies_before]
    changed = (volume_before - volume_after) if cut else (volume_after - volume_before)
    if cut and changed <= 1e-6 * max(volume_before, 1.0):
        raise SolidWorksError(f"The cut toward {list(toward)} removed no material: that side of the sketch is "
                              "empty. Rolled back.")
    if not cut and merge and changed <= 1e-6 and not new_bodies:
        raise SolidWorksError(f"The extrusion toward {list(toward)} added no material. Rolled back.")
    result["volume_change_mm3"] = round(-changed if cut else changed, 3)
    result["dimensions"] = {"depth": f"D1@{result['feature']}"} if t1 == SW_END_COND_BLIND else {}
    if region is not None:
        result["region_area_mm2"] = round(offset_loop.polygon_area(region["polygon"]) * 1e6, 3)
    if not merge and new_bodies:
        result["body"] = new_bodies[0]
    return result


def extrude(session, sketch: str, toward, depth_mm=None, through_all: bool = False, up_to=None,
            merge: bool = True, region_point_mm=None, name: str | None = None) -> dict:
    """Boss-extrude a sketch (or the region of it around region_point_mm) toward a model direction."""
    return _make(session, sketch, False, toward, depth_mm, through_all, up_to, merge, region_point_mm, name)


def cut(session, sketch: str, toward, depth_mm=None, through_all: bool = False, up_to=None,
        region_point_mm=None, name: str | None = None) -> dict:
    """Cut-extrude a sketch (or the region of it around region_point_mm) toward a model direction."""
    return _make(session, sketch, True, toward, depth_mm, through_all, up_to, True, region_point_mm, name)
