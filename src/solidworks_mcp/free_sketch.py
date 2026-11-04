"""Free sketches: a chain of lines, arcs and splines, fully defined.

build_chain checks the segments and works out their geometry: arc centres,
tangent arcs, which segments meet smoothly. plan_chain then picks what fully
defines the sketch, the way sketch_constraints does for polygons: relations
only where the input already holds them exactly (so nothing moves), then one
dimension from the origin per remaining degree of freedom. Which relation or
dimension still removes a degree of freedom follows from the rank of the
constraints' gradients, so one that merely repeats the others (a horizontal
side that a tangent already keeps level) is left out instead of
over-defining the sketch. Both are pure (unit-tested); the session draws.
"""

import math
from dataclasses import dataclass, field

from .errors import SolidWorksError

EXACT_MM = 1e-9  # relations and origin ties only where the input is this exact
MERGE_MM = 1e-4  # SolidWorks merged sketch points 1e-5 mm apart, not 1e-4 (verified): one point here too
TANGENT_MM = 1e-3  # how far a "tangent" line's end may lie off the tangent; it is put on it
# A row that adds less than this to the rank holds nothing SolidWorks counts:
# concentric arcs whose chords share a bisector held their centre by 2e-6, and
# SolidWorks saw it free
_RANK_TOLERANCE = 1e-4
_STEP_MM = 1e-6  # central differences: exact for the quadratic constraints, close for a radius


@dataclass
class Chain:
    """The geometry of a chain, in sketch coordinates (mm).

    points: every sketch point, the given ones first (start, then each segment's
    through points and end), then the arc centres. entities: one per segment,
    ("line", a, b), ("arc", start, end, centre, direction) with direction 1 =
    counterclockwise, -1 = clockwise, or ("spline", [point, ...]). smooth:
    (entity, next entity, point) where two segments meet tangentially.
    """
    points: list
    entities: list
    user_points: list
    closed: bool
    smooth: list = field(default_factory=list)


def _point(value, what: str) -> tuple:
    try:
        u, v = value
        return float(u), float(v)
    except (TypeError, ValueError):
        raise SolidWorksError(f"{what} must be a point [u, v] (got {value}).") from None


def _unit(dx: float, dy: float) -> tuple:
    length = math.hypot(dx, dy)
    return dx / length, dy / length


def _cross(a, b) -> float:
    return a[0] * b[1] - a[1] * b[0]


def _fmt(p) -> str:
    return f"({', '.join(f'{c:g}' for c in p)})"


def _direction(points, entity, at: int, centre=None):
    """The direction of travel of a line or arc at one of its points; None for
    a spline. An arc's centre comes from `centre` while the chain is built."""
    if entity[0] == "line":
        (u1, v1), (u2, v2) = points[entity[1]], points[entity[2]]
        return _unit(u2 - u1, v2 - v1)
    if entity[0] == "arc":
        (pu, pv), (cu, cv) = points[at], centre or points[entity[3]]
        return _unit(-entity[4] * (pv - cv), entity[4] * (pu - cu))
    return None


def _circumcentre(a, b, c):
    d = 2 * (a[0] * (b[1] - c[1]) + b[0] * (c[1] - a[1]) + c[0] * (a[1] - b[1]))
    sa, sb, sc = a[0] ** 2 + a[1] ** 2, b[0] ** 2 + b[1] ** 2, c[0] ** 2 + c[1] ** 2
    return ((sa * (b[1] - c[1]) + sb * (c[1] - a[1]) + sc * (a[1] - b[1])) / d,
            (sa * (c[0] - b[0]) + sb * (a[0] - c[0]) + sc * (b[0] - a[0])) / d)


def build_chain(start_mm, segments) -> Chain:
    """The chain's geometry from a start point and segments, each running on
    from where the last ended: {"line": [u, v]}, {"arc": [u, v], "through":
    [u, v]}, {"arc": [u, v], "center": [u, v]} (the short way round),
    {"arc": [u, v], "tangent": true}, {"spline": [[u, v], ...]}. A line may say
    "tangent": true too. Ending on the start closes the chain. Points closer
    than MERGE_MM are one point, and an arc centre that close to the origin
    goes onto it."""
    if not segments:
        raise SolidWorksError("A sketch needs at least one segment.")
    start = _point(start_mm, "start_mm")
    points, entities, smooth = [start], [], []
    closed = False

    def add_point(p, last: bool) -> int:
        nonlocal closed
        for index, known in enumerate(points):
            if math.dist(known, p) <= MERGE_MM:
                if index == 0 and last:
                    closed = True
                    return 0
                raise SolidWorksError(f"The chain passes {_fmt(p)} twice; a sketch outline may not cross itself.")
        points.append(p)
        return len(points) - 1

    current = 0
    centres = []  # (entity, (u, v)), indexed after the given points
    for k, segment in enumerate(segments):
        last = k == len(segments) - 1
        kinds = [key for key in ("line", "arc", "spline") if isinstance(segment, dict) and key in segment]
        if len(kinds) != 1:
            raise SolidWorksError(f"Segment {k} must be one of line, arc or spline (got {segment}).")
        kind, tangent = kinds[0], bool(segment.get("tangent", False))
        here = points[current]
        incoming = None
        if tangent:
            if k == 0:
                raise SolidWorksError("The first segment cannot be tangent: nothing comes before it.")
            incoming = _direction(points, entities[-1], current, centres[-1][1] if entities[-1][0] == "arc" else None)
            if incoming is None:
                raise SolidWorksError(f"Segment {k} cannot be tangent to a spline; end the spline in a line or arc.")

        if kind == "spline":
            if tangent:
                raise SolidWorksError(f"A spline (segment {k}) cannot be tangent; its ends follow its points.")
            through = segment["spline"]
            if not isinstance(through, (list, tuple)) or not through:
                raise SolidWorksError(f"Segment {k}: a spline runs through [[u, v], ...] (got {through}).")
            route = [_point(p, f"Segment {k}'s spline point") for p in through]
            indices = [current] + [add_point(p, last and i == len(route) - 1) for i, p in enumerate(route)]
            entities.append(("spline", indices))
            current = indices[-1]
            continue

        end = _point(segment[kind], f"Segment {k}'s end")
        if math.dist(here, end) <= MERGE_MM:
            raise SolidWorksError(f"Segment {k} has zero length: it ends where it starts, at {_fmt(end)}.")
        if kind == "line":
            if tangent:
                offset = _cross(incoming, (end[0] - here[0], end[1] - here[1]))
                along = incoming[0] * (end[0] - here[0]) + incoming[1] * (end[1] - here[1])
                if abs(offset) > TANGENT_MM or along <= 0:
                    raise SolidWorksError(f"Segment {k}'s line to {_fmt(end)} does not continue the segment before "
                                          f"tangentially: from {_fmt(here)} it must run along {_fmt(incoming)}.")
                end = (here[0] + along * incoming[0], here[1] + along * incoming[1])
            index = add_point(end, last)
            entities.append(("line", current, index))
        else:
            if tangent and "center" in segment:
                raise SolidWorksError(f"Segment {k}: an arc takes a center or tangent: true, not both.")
            if tangent:
                left = (-incoming[1], incoming[0])
                d = (end[0] - here[0], end[1] - here[1])
                across = 2 * (left[0] * d[0] + left[1] * d[1])
                if abs(across) <= EXACT_MM * max(1.0, math.hypot(*d)):
                    raise SolidWorksError(f"Segment {k}: a tangent arc to {_fmt(end)} would be straight; use a line.")
                s = (d[0] ** 2 + d[1] ** 2) / across
                centre, direction = (here[0] + s * left[0], here[1] + s * left[1]), 1 if s > 0 else -1
            elif "center" in segment:
                centre = _point(segment["center"], f"Segment {k}'s center")
                radius, reach = math.dist(here, centre), math.dist(end, centre)
                if abs(reach - radius) > TANGENT_MM or radius <= MERGE_MM:
                    raise SolidWorksError(f"Segment {k}: {_fmt(here)} and {_fmt(end)} are not on one circle round "
                                          f"{_fmt(centre)}: they lie {radius:g} and {reach:g} from it.")
                out = _unit(end[0] - centre[0], end[1] - centre[1])
                end = (centre[0] + radius * out[0], centre[1] + radius * out[1])  # onto the circle, as asked
                turn = _cross((here[0] - centre[0], here[1] - centre[1]), (end[0] - centre[0], end[1] - centre[1]))
                if abs(turn) <= EXACT_MM * radius ** 2:
                    raise SolidWorksError(f"Segment {k}: round {_fmt(centre)} from {_fmt(here)} to {_fmt(end)} is a "
                                          "half circle either way; give a \"through\" point instead.")
                direction = 1 if turn > 0 else -1
            elif "through" in segment:
                middle = _point(segment["through"], f"Segment {k}'s through point")
                turn = _cross((middle[0] - here[0], middle[1] - here[1]), (end[0] - middle[0], end[1] - middle[1]))
                if abs(turn) <= EXACT_MM * max(1.0, math.dist(here, end)) ** 2:
                    raise SolidWorksError(f"Segment {k}: {_fmt(here)}, {_fmt(middle)} and {_fmt(end)} lie in one "
                                          "line; an arc needs a point off it.")
                centre, direction = _circumcentre(here, middle, end), 1 if turn > 0 else -1
            else:
                raise SolidWorksError(f"Segment {k}: an arc needs a \"through\" point, a \"center\" or \"tangent\": true.")
            index = add_point(end, last)
            entities.append(("arc", current, index, None, direction))
            centres.append((len(entities) - 1, centre))
        if tangent:
            smooth.append((len(entities) - 2, len(entities) - 1, current))
        current = index

    user = len(points)
    for entity, centre in centres:
        match = next((i for i, p in enumerate(points) if math.dist(p, centre) <= MERGE_MM), None)
        if match is None:
            if math.hypot(*centre) <= MERGE_MM:
                centre = (0.0, 0.0)  # on the origin: a relation there, not a hair beside it
            points.append(centre)
            match = len(points) - 1
        kind, a, b, _, direction = entities[entity]
        entities[entity] = (kind, a, b, match, direction)

    joints = [(k, k + 1, entities[k + 1][1] if entities[k + 1][0] != "spline" else entities[k + 1][1][0])
              for k in range(len(entities) - 1)]
    if closed and len(entities) > 1:
        joints.append((len(entities) - 1, 0, 0))
    told = {(a, b) for a, b, _ in smooth}
    for a, b, at in joints:
        if (a, b) in told:
            continue
        before, after = _direction(points, entities[a], at), _direction(points, entities[b], at)
        if before and after and abs(_cross(before, after)) <= EXACT_MM and before[0] * after[0] + before[1] * after[1] > 0:
            smooth.append((a, b, at))
    return Chain(points, entities, list(range(user)), closed, smooth)


@dataclass
class ChainPlan:
    """What fully defines a chain. relations: ("tangent" | "collinear", entity,
    entity), ("horizontal" | "vertical", entity), ("at_origin" | "origin_x" |
    "origin_y", point). dimensions: ("x" | "y", point, value) from the origin, or
    ("radius", entity, value)."""
    relations: list
    dimensions: list
    fully_defined: bool
    user_count: int
    centre_of: dict  # centre point -> its arc entity

    def role(self, dimension) -> str:
        kind, index, _ = dimension
        if kind == "radius":
            return f"r{index}"
        if index < self.user_count:
            return f"{kind}{index}"
        return f"centre{self.centre_of[index]}_{kind}"


class _Rank:
    """Rows kept only while they add to the rank (Gram-Schmidt on unit rows)."""

    def __init__(self) -> None:
        self.basis = []

    def add(self, row) -> bool:
        norm = math.sqrt(sum(x * x for x in row))
        if norm == 0.0:
            return False
        v = [x / norm for x in row]
        for _ in range(2):  # twice, for stability
            for b in self.basis:
                d = sum(x * y for x, y in zip(v, b))
                v = [x - d * y for x, y in zip(v, b)]
        rest = math.sqrt(sum(x * x for x in v))
        if rest < _RANK_TOLERANCE:
            return False
        self.basis.append([x / rest for x in v])
        return True


def _row(size: int, variables, f, values) -> list:
    """The gradient of f over the given unknowns (central differences), as a full row."""
    row = [0.0] * size
    for k, var in enumerate(variables):
        up, down = list(values), list(values)
        up[k] += _STEP_MM
        down[k] -= _STEP_MM
        row[var] += (f(up) - f(down)) / (2 * _STEP_MM)
    return row


def plan_chain(chain: Chain, origin_mm: tuple = (0.0, 0.0)) -> ChainPlan:
    """Relations and dimensions that remove every degree of freedom of the chain once.

    origin_mm is where the model origin lies in the sketch's coordinates — the
    anchor relations and dimensions SolidWorks ties to it are decided relative
    to that point, which sits away from the sketch's (0, 0) on a plane that
    does not contain the model origin."""
    u0, v0 = origin_mm
    pts = [(p[0] - u0, p[1] - v0) for p in chain.points]
    size = 2 * len(pts)
    rank = _Rank()

    def u(i):
        return 2 * i

    def v(i):
        return 2 * i + 1

    def keep(variables, f) -> bool:
        values = [pts[var // 2][var % 2] for var in variables]
        return rank.add(_row(size, variables, f, values))

    def towards(entity, at):
        """Variables and a function giving the entity's direction at point `at`, up to length."""
        if entity[0] == "line":
            other = entity[2] if entity[1] == at else entity[1]
            return [u(at), v(at), u(other), v(other)], lambda w: (w[2] - w[0], w[3] - w[1])
        centre = entity[3]
        return [u(at), v(at), u(centre), v(centre)], lambda w: (-(w[1] - w[3]), w[0] - w[2])

    for kind, *rest in chain.entities:  # an arc keeps its ends at one radius
        if kind == "arc":
            s, e, c, _ = rest
            keep([u(s), v(s), u(e), v(e), u(c), v(c)],
                 lambda w: (w[0] - w[4]) ** 2 + (w[1] - w[5]) ** 2 - (w[2] - w[4]) ** 2 - (w[3] - w[5]) ** 2)

    relations = []
    for a, b, at in chain.smooth:
        (vars_a, dir_a), (vars_b, dir_b) = towards(chain.entities[a], at), towards(chain.entities[b], at)
        n = len(vars_a)
        if keep(vars_a + vars_b, lambda w: _cross(dir_a(w[:n]), dir_b(w[n:]))):
            both_lines = chain.entities[a][0] == chain.entities[b][0] == "line"
            relations.append(("collinear" if both_lines else "tangent", a, b))

    for index, entity in enumerate(chain.entities):
        if entity[0] != "line":
            continue
        (u1, v1), (u2, v2) = pts[entity[1]], pts[entity[2]]
        a, b = entity[1], entity[2]
        if abs(v1 - v2) <= EXACT_MM and keep([v(a), v(b)], lambda w: w[0] - w[1]):
            relations.append(("horizontal", index))
        elif abs(u1 - u2) <= EXACT_MM and keep([u(a), u(b)], lambda w: w[0] - w[1]):
            relations.append(("vertical", index))

    dimensions = []

    def anchor(i):
        """Tie or dimension both coordinates of point i, where that still counts:
        a relation to the origin at 0 (there is no 0 mm dimension), else a dimension."""
        at_zero = [abs(pts[i][axis]) <= EXACT_MM for axis in (0, 1)]
        counts = [keep([var], lambda w: w[0]) for var in (u(i), v(i))]
        if all(at_zero) and all(counts):
            relations.append(("at_origin", i))
            return
        for axis in (0, 1):
            if counts[axis] and at_zero[axis]:
                relations.append((("origin_x", "origin_y")[axis], i))
            elif counts[axis]:
                dimensions.append((("x", "y")[axis], i, pts[i][axis]))

    # the point on the origin first, also an arc centre: tied there, it carries the intent
    origin = next((i for i in range(len(pts)) if math.hypot(*pts[i]) <= EXACT_MM), None)
    if origin is not None:
        anchor(origin)
    for i in chain.user_points:
        if i != origin:
            anchor(i)
    centre_of = {}
    for index, entity in enumerate(chain.entities):
        if entity[0] == "arc":
            centre_of[entity[3]] = index
            s, c = entity[1], entity[3]
            if keep([u(s), v(s), u(c), v(c)], lambda w: math.hypot(w[0] - w[2], w[1] - w[3])):
                dimensions.append(("radius", index, math.dist(pts[s], pts[c])))
    for centre in sorted(centre_of):
        if centre >= len(chain.user_points):
            anchor(centre)
    return ChainPlan(relations, dimensions, len(rank.basis) == size, len(chain.user_points), centre_of)
