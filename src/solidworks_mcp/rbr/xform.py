"""A component's transform (IMathTransform.ArrayData) applied in Python; pure, unit-tested.

ArrayData holds 16 doubles: a 3x3 rotation (0..8), the translation (9..11), the scale (12)
and three unused. Whether a point is a row vector times the matrix ('row') or the matrix
times a column vector ('col') is checked once per call against IMathPoint.MultiplyTransform
(convention_of), so a wrong guess can never move a ray."""


def _parts(arr):
    a = [float(v) for v in list(arr)[:13]]
    if len(a) < 12:
        raise ValueError("A transform needs at least 12 values (rotation, translation).")
    rot = [a[0:3], a[3:6], a[6:9]]
    scale = a[12] if len(a) > 12 and a[12] else 1.0
    return rot, a[9:12], scale


def rotate(arr, v, convention: str = "row") -> list:
    rot, _, _ = _parts(arr)
    if convention == "row":
        return [sum(v[i] * rot[i][j] for i in range(3)) for j in range(3)]
    return [sum(rot[j][i] * v[i] for i in range(3)) for j in range(3)]


def unrotate(arr, v, convention: str = "row") -> list:
    """The inverse rotation (the transpose of an orthonormal matrix)."""
    rot, _, _ = _parts(arr)
    if convention == "row":
        return [sum(v[j] * rot[i][j] for j in range(3)) for i in range(3)]
    return [sum(rot[j][i] * v[j] for j in range(3)) for i in range(3)]


def apply(arr, p, convention: str = "row") -> list:
    """Component space -> assembly space."""
    _, t, s = _parts(arr)
    r = rotate(arr, p, convention)
    return [s * r[k] + t[k] for k in range(3)]


def invert(arr, p, convention: str = "row") -> list:
    """Assembly space -> component space."""
    _, t, s = _parts(arr)
    return unrotate(arr, [(p[k] - t[k]) / s for k in range(3)], convention)


def convention_of(arr, probe, transformed, tolerance: float = 1e-9) -> str:
    """'row' or 'col': which reading of arr maps probe onto transformed (what SolidWorks'
    own MultiplyTransform gave); ValueError when neither does."""
    for convention in ("row", "col"):
        if all(abs(a - b) <= tolerance * max(1.0, abs(b)) for a, b in zip(apply(arr, probe, convention), transformed)):
            return convention
    raise ValueError("The component's transform matches neither reading of its matrix.")
