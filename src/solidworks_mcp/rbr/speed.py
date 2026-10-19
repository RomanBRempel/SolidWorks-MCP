"""Speed patches found in the journal of a real session (GAT-27 v5, 113 features, 2026-10-19).

- add_fillet refusal: upstream tries every selected edge alone (and with
  propagation, and bisects the largest radius) to say which edges fail; on a
  48-edge selection that took 99.7 s. Here the same diagnosis runs within
  SWMCP_FILLET_DIAG_S seconds (15) and names the edges it had no time for.
- list_edges with within_mm / min_length_mm: upstream built the full entry
  (curve, type, ends, length: 4 COM calls) for every edge of the body before
  filtering, 46-49 s per call. Here one GetCurveParams2 per edge filters
  first; full entries only for the edges kept. Same indices, same entries.
- rebuild_profile: per-feature rebuild times (IFeatureStatistics), to find the
  few features behind a slow rebuild.

Upstream code adapted here (MIT, Henk Jan Baard): _fillet_refusal, list_edges.
"""

import math
import os
import time

from .. import binding
from ..errors import SolidWorksError
from ..session import SolidWorksSession

FILLET_DIAG_S = float(os.environ.get("SWMCP_FILLET_DIAG_S", "15"))
SW_DELETE_ABSORBED = 1

_ORIGINAL = {}


class _OutOfTime(Exception):
    pass


def _fillet_refusal(self, radius_mm: float, edges: list, propagate: bool = False) -> str:
    """Upstream's diagnosis of a refused fillet, within FILLET_DIAG_S seconds."""
    if len(edges) > self._MAX_FILLET_TRIALS:
        return f"SolidWorks could not round {len(edges)} edge(s) with R{radius_mm:g}: is the radius too large?"
    deadline = time.perf_counter() + FILLET_DIAG_S
    model = self._model
    extension = binding.wrap(model.Extension, self._mod.IModelDocExtension)
    index_of = {ref: i for i, ref in enumerate(self._persist_refs(self._solid_body().GetEdges() or ()))}

    def rounds(refs, chain=propagate, radius=radius_mm) -> bool:
        if time.perf_counter() > deadline:
            raise _OutOfTime
        model.ClearSelection2(True)
        for ref in refs:
            binding.wrap(extension.GetObjectByPersistReference3(ref)[0], self._mod.IEntity).Select4(True, None)
        trial = self._uniform_fillet(radius, chain)
        model.ClearSelection2(True)
        if trial is None:
            return False
        binding.wrap(trial, self._mod.IFeature).Select2(False, 0)
        extension.DeleteSelection2(SW_DELETE_ABSORBED)
        model.ClearSelection2(True)
        return True

    refs = self._persist_refs(edges)
    fits, alone, together, unchecked = [], [], [], []
    for n, ref in enumerate(refs):
        try:
            if rounds(fits + [ref]):
                fits.append(ref)
            else:
                (together if rounds([ref]) else alone).append(ref)
        except _OutOfTime:
            unchecked = refs[n:]
            break
    chained = []
    if not propagate:
        for ref in alone:
            try:
                if rounds([ref], True):
                    chained.append(ref)
            except _OutOfTime:
                break
    alone = [ref for ref in alone if ref not in chained]

    def named(group) -> str:
        return ", ".join(f"{index_of.get(r, -1)} ({self._edge_entry(0, extension.GetObjectByPersistReference3(r)[0])['length_mm']:g} mm)"
                         for r in group)

    parts = []
    if chained:
        parts.append(f"edge(s) {named(chained)} fail alone but round with tangent_propagation=True: they end where "
                     "they run on smoothly into further edges, such as round a fillet, which the round must follow")
    if alone:
        largest = ""
        if not unchecked:
            def try_radius(ref, r):
                return rounds([ref], radius=r)
            try:
                largest = self._largest_rounds(alone, index_of, try_radius, radius_mm)
            except _OutOfTime:
                largest = ""
        parts.append(f"edge(s) {named(alone)} fail even alone{'' if propagate else ', also with tangent propagation'}"
                     + largest)
    if together:
        parts.append(f"edge(s) {named(together)} round alone but not with the others")
    if unchecked:
        parts.append(f"{len(unchecked)} edge(s) not checked within {FILLET_DIAG_S:g} s: "
                     + ", ".join(str(index_of.get(r, -1)) for r in unchecked[:30])
                     + (" ..." if len(unchecked) > 30 else ""))
    message = f"R{radius_mm:g} does not round all {len(edges)} edge(s): {'; '.join(parts) or 'no single edge found'}."
    if fits:
        keep = ",".join(str(index_of.get(r, -1)) for r in fits)
        message += f" These {len(fits)} round together: edges=\"{keep}\" (list_edges indices)."
    return message + " Else a smaller radius, or skip_shorter_mm for slivers."


def _ends_of(self, edge_dispatch):
    """(start, end, chord) in mm from one GetCurveParams2 call."""
    params = binding.wrap(edge_dispatch, self._mod.IEdge).GetCurveParams2()
    start = [v * 1000.0 for v in params[0:3]]
    end = [v * 1000.0 for v in params[3:6]]
    return start, end, math.dist(start, end)


def list_edges(self, face=None, feature: str | None = None, within_mm=None,
               min_length_mm: float | None = None) -> dict:
    """Upstream list_edges, filtering on each edge's ends first (one COM call per edge)."""
    self._require_model()
    body = self._solid_body()
    edges = list(body.GetEdges() or ())
    chosen = range(len(edges))
    if face is not None or feature is not None:
        index_of = {ref: i for i, ref in enumerate(self._persist_refs(edges))}
        for picked in (self._face_edges(body, face) if face is not None else None,
                       self._feature_edges(feature) if feature is not None else None):
            if picked is not None:
                keep = {index_of[ref] for ref in self._persist_refs(picked) if ref in index_of}
                chosen = [i for i in chosen if i in keep]
    box = self._box_corners(within_mm) if within_mm is not None else None
    if box is not None or min_length_mm is not None:
        kept = []
        for i in chosen:
            start, end, chord = _ends_of(self, edges[i])
            points = [start, end] if chord > 1e-6 else [start]
            if box is not None and not all(all(lo - 1e-4 <= c <= hi + 1e-4 for c, lo, hi in zip(p, *box))
                                           for p in points):
                continue
            kept.append(i)  # the length check needs the curve: done on the full entry below
        chosen = kept
    out = []
    for i in chosen:
        entry = self._edge_entry(i, edges[i])
        if min_length_mm is not None and entry["length_mm"] < min_length_mm:
            continue
        if box is not None and not all(all(lo <= c <= hi for c, lo, hi in zip(p, *box))
                                       for p in entry.get("ends_mm", [entry["start_mm"]])):
            continue  # the exact upstream test on the rounded values decides at the margin
        entry.pop("start_mm")
        out.append(entry)
    return {"ok": True, "count": len(out), "edges": out}


def rebuild_profile(session, top: int = 15) -> dict:
    """Rebuild the current part once and report how long each feature takes."""
    model = session._require_model()
    ext = binding.wrap(model.Extension, session._mod.IModelDocExtension)
    stats = binding.wrap(ext.FeatureStatistics, session._mod.IFeatureStatistics)
    if stats is None:
        raise SolidWorksError("SolidWorks gave no feature statistics for this document.")
    t0 = time.perf_counter()
    stats.Refresh()
    wall = time.perf_counter() - t0
    names = list(stats.FeatureNames or ())
    times = list(stats.FeatureUpdateTimes or ())
    shares = list(stats.FeatureUpdatePercentageTimes or ())
    rows = sorted(({"feature": n, "s": round(float(t), 3), "share_pct": round(float(p), 1)}
                   for n, t, p in zip(names, times, shares)), key=lambda r: -r["s"])
    return {"ok": True, "document": model.GetTitle(), "features": int(stats.FeatureCount or len(names)),
            "total_rebuild_s": round(float(stats.TotalRebuildTime or 0), 3), "wall_s": round(wall, 2),
            "slowest": rows[:max(1, int(top))],
            "hint": "Suppress the slowest features (fillets, shells, lofts) while iterating on others, "
                    "or move them to the end of the tree; unsuppress before the final check."}


def apply() -> None:
    if _ORIGINAL:
        return
    for name, fn in (("_fillet_refusal", _fillet_refusal), ("list_edges", list_edges)):
        _ORIGINAL[name] = getattr(SolidWorksSession, name)
        fn.__wrapped__ = _ORIGINAL[name]
        fn.__doc__ = _ORIGINAL[name].__doc__
        setattr(SolidWorksSession, name, fn)
