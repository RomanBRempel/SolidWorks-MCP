"""Feature tree in one pass: counts by type, errors, suppressed, the last features (part or assembly)."""
from collections import Counter

feats = list(session._iter_features())
types, errors, suppressed, names = Counter(), [], [], []
for f in feats:
    t = f.GetTypeName2()
    types[t] += 1
    names.append(f.Name)
    code, warning = f.GetErrorCode2()
    if code:
        errors.append({"name": f.Name, "type": t, "code": code, "warning": bool(warning)})
    try:
        if f.IsSuppressed2(1, None)[0]:
            suppressed.append(f.Name)
    except Exception:
        pass
result = {"document": model.GetTitle(), "count": len(feats), "types": dict(types.most_common()),
          "errors": errors, "suppressed": suppressed, "last": names[-int(args.get("last", 10)):]}
