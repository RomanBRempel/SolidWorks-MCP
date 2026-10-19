"""Export every unique part of the current assembly to a folder: args {folder, format: 'step'|'stl'|'x_t'}."""
import os

folder = args["folder"]
fmt = args.get("format", "step").lower().lstrip(".")
os.makedirs(folder, exist_ok=True)
done, failed = {}, []
for raw in asm.GetComponents(False) or ():
    c = wrap(raw, mod.IComponent2)
    doc = wrap(c.GetModelDoc2(), mod.IModelDoc2)
    if doc is None or int(doc.GetType()) != 1:
        continue
    src = doc.GetPathName()
    if not src or src in done:
        continue
    target = os.path.join(folder, os.path.splitext(os.path.basename(src))[0] + "." + fmt)
    e = wrap(doc.Extension, mod.IModelDocExtension)
    ok = e.SaveAs3(target, 0, 1 | 2, None, None, 0, 0) if hasattr(e, "SaveAs3") else e.SaveAs(target, 0, 1 | 2, None, 0, 0)
    ok = ok[0] if isinstance(ok, tuple) else ok
    (done.__setitem__(src, target) if ok and os.path.isfile(target) else failed.append(src))
result = {"exported": len(done), "failed": failed, "folder": folder, "files": list(done.values())}
