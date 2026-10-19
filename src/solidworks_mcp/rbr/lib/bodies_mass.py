"""Solid bodies of the current part with volume (mm^3) and mass (g) at the part's material density."""
bodies = part.GetBodies2(0, True) or ()
if not isinstance(bodies, (list, tuple)):
    bodies = [bodies]
ext_props = ext.GetMassProperties2(1, 0, False)
out, total_v = [], 0.0
for b in bodies:
    b = wrap(b, mod.IBody2)
    mp = b.GetMassProperties(1.0)  # density 1 -> mass == volume (m^3)
    v = mp[3] * 1e9
    total_v += v
    out.append({"name": b.Name, "volume_mm3": round(v, 2)})
mass_g = ext_props[5] * 1000 if ext_props else None
density = (mass_g / total_v) if (mass_g and total_v) else None  # g/mm^3
for o in out:
    o["mass_g"] = round(o["volume_mm3"] * density, 2) if density else None
result = {"bodies": out, "total_volume_mm3": round(total_v, 2), "part_mass_g": round(mass_g, 2) if mass_g else None}
