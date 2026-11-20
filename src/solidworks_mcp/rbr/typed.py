"""One way to call the SolidWorks API from a script, whatever object the call starts on.

A script meets two kinds of COM objects. Those the type library names (swApp, a
feature from feature()) are early-bound: methods are methods, out-parameters
come back in a tuple. Everything the type library only calls "IDispatch" -
swApp.ActiveDoc, the items of GetParents / GetFaces / GetComponents, what
GetSpecificFeature2 returns - arrives late-bound, and there the same API behaves
differently: a method without arguments is run by merely naming it
(doc.GetTitle is already the string, doc.GetTitle() dies with "'str' object is
not callable"), Select4 / SelectByID2 / Save3 / OpenDoc6 refuse plain arguments
with "type mismatch". Which kind a script got depended on where the object came
from, so the same line worked in one script and failed in the next (journal
2026-11-20).

Auto wraps any COM object and resolves every member by NAME against the
generated type library: the interfaces that have a member of that name are
asked of the object (QueryInterface) and the first it supports is used,
early-bound. What a call returns is wrapped the same way, so the whole chain
behaves as the SolidWorks API help describes: methods are called with
parentheses, out-parameters return (retval, out1, ...).

A call written the late-bound way - a VARIANT(VT_BYREF | ...) passed for each
out-parameter - is honoured as such: the VARIANTs are filled and the call
returns the return value alone.

Objects that are not SolidWorks' own - an add-in's API reached through
swApp.GetAddInObject("FloWorks.App"), SolidCAM, Simulation - support none of
the type library's interfaces. Their members are resolved by the object itself
(late binding, as before this module existed), so scripts written against
those APIs run unchanged; such objects describe themselves (they carry type
info), which is what makes late binding dependable there and not on
SolidWorks' own objects.

Everything here runs on the COM thread. index(), ranked(), Member's argument
handling and the fallbacks are pure and unit-tested with stand-in classes.
"""

import re

import pythoncom
import win32com.client

from ..errors import SolidWorksError

_BASE = getattr(win32com.client, "DispatchBaseClass", None)
_VARIANT = getattr(win32com.client, "VARIANT", None)
if not isinstance(_VARIANT, type):   # the import stub of the non-Windows tests
    _VARIANT = None
_IID_IDISPATCH = getattr(pythoncom, "IID_IDispatch", None)
_VT_BYREF = getattr(pythoncom, "VT_BYREF", 0x4000)
E_NOINTERFACE = -2147467262
# What a makepy class or win32com itself calls on the dispatch it holds: an Auto handed to
# mod.IFeature(obj) or win32com.client.Dispatch(obj) must answer these as the dispatch does.
DISPATCH_MEMBERS = frozenset({"InvokeTypes", "Invoke", "GetIDsOfNames", "QueryInterface", "GetTypeInfo",
                              "GetTypeInfoCount"})


def _late_bound(oleobj):
    """The dispatch as win32com's dynamic (late-bound) object; replaced in the tests."""
    return win32com.client.dynamic.Dispatch(oleobj)

# Asked first when several interfaces have the member: the ones scripts live on.
PREFERRED = (
    "ISldWorks", "IModelDoc2", "IPartDoc", "IAssemblyDoc", "IDrawingDoc", "IModelDocExtension",
    "IFeatureManager", "ISelectionMgr", "ISketchManager", "IFeature", "ISketch", "ISketchSegment",
    "ISketchPoint", "ISketchLine", "ISketchArc", "ISketchSpline", "ISketchRelation", "IBody2", "IFace2", "IEdge",
    "IVertex", "IEntity", "IComponent2", "IDisplayDimension", "IDimension", "IConfiguration", "IEquationMgr",
    "IRefPlane", "ISurface", "ICurve", "IMathUtility", "IMathTransform", "IMathPoint", "IMathVector", "IView",
    "ISheet", "IMate2", "ICustomPropertyManager", "IModelView",
)
_RANK = {name: i for i, name in enumerate(PREFERRED)}
_INDEX: dict = {}   # id(module) -> {member: (classes, ...)}
_KNOWN: dict = {}   # id(module) -> frozenset of its interface classes


def members(cls) -> set:
    """The API members a generated interface class has: methods, and properties to get or put."""
    names = {n for n, v in vars(cls).items() if not n.startswith("_") and callable(v)}
    names |= set(getattr(cls, "_prop_map_get_", None) or ())
    names |= set(getattr(cls, "_prop_map_put_", None) or ())
    return names


def has(cls, name: str) -> bool:
    return (name in vars(cls) and callable(vars(cls)[name])) \
        or name in (getattr(cls, "_prop_map_get_", None) or ()) \
        or name in (getattr(cls, "_prop_map_put_", None) or ())


def _family(name: str) -> tuple:
    """('IFace', 2) for 'IFace2': the newest of a family is asked first."""
    match = re.match(r"^(.*?)(\d*)$", name)
    return match.group(1), int(match.group(2) or 0)


def ranked(classes) -> list:
    """Interface classes in the order they are asked of an object: the preferred ones, then
    by name with the newest version of a family (IBody2 before IBody) first."""
    def key(cls):
        family, version = _family(cls.__name__)
        return (_RANK.get(cls.__name__, len(_RANK)), family, -version)
    return sorted(classes, key=key)


def interfaces(mod) -> list:
    """The generated interface classes of a type library module."""
    if _BASE is None:
        return []
    return [c for c in vars(mod).values()
            if isinstance(c, type) and issubclass(c, _BASE) and c is not _BASE and getattr(c, "CLSID", None)]


def index(mod, classes=None) -> dict:
    """{member name: interface classes that have it, in asking order}; built once per module.
    `classes` (tests) replaces the module's own interface classes."""
    cached = _INDEX.get(id(mod)) if classes is None else None
    if cached is not None:
        return cached
    found_classes = list(interfaces(mod) if classes is None else classes)
    table: dict = {}
    for cls in found_classes:
        for name in members(cls):
            table.setdefault(name, []).append(cls)
    out = {name: tuple(ranked(found)) for name, found in table.items()}
    _INDEX[id(mod)] = out
    _KNOWN[id(mod)] = frozenset(found_classes)
    return out


def known(mod) -> frozenset:
    """The interface classes of the module (after index())."""
    if id(mod) not in _KNOWN:
        index(mod)
    return _KNOWN[id(mod)]


def is_com(value) -> bool:
    """A COM object in any dress: Auto, a generated class, a dynamic dispatch, a bare PyIDispatch."""
    if isinstance(value, Auto):
        return True
    if getattr(type(value), "__name__", "") == "PyIDispatch":
        return True
    return hasattr(value, "_oleobj_") and not isinstance(value, type)


def bare(value):
    """What a COM call takes for an argument: Auto unwrapped, also inside lists and tuples."""
    if isinstance(value, Auto):
        return value._bound_ if value._bound_ is not None else value._oleobj_
    if isinstance(value, list):
        return [bare(v) for v in value]
    if isinstance(value, tuple):
        return tuple(bare(v) for v in value)
    return value


def _byref(value) -> bool:
    if _VARIANT is None or not isinstance(value, _VARIANT):
        return False
    try:
        return bool(int(value.varianttype) & _VT_BYREF)
    except Exception:  # noqa: BLE001
        return False


def plain_arguments(args) -> tuple:
    """(arguments for the early-bound method, the by-reference VARIANTs among them).

    A late-bound call passes VARIANT(VT_BYREF | VT_I4, 0) for an out-parameter; the
    early-bound method wants the plain value there and returns the result in a tuple."""
    plain, outs = [], []
    for a in args:
        if _byref(a):
            outs.append(a)
            plain.append(bare(a.value))
        else:
            plain.append(bare(a))
    return tuple(plain), outs


def give_back(result, outs: list):
    """After a call that was given by-reference VARIANTs: fill them from the tuple the
    early-bound method returned and hand back the return value alone, as late binding does."""
    if not outs or not isinstance(result, tuple) or len(result) < 2:
        return result
    for variant, value in zip(outs, result[1:]):
        try:
            variant.value = value
        except Exception:  # noqa: BLE001 - the return value still stands
            pass
    return result[0]


class Member:
    """A method of a COM object, bound: call it. Shown as text it says so - a late-bound
    object used to run a method without arguments when it was merely named."""

    __slots__ = ("_owner", "_name", "_fn", "_interface")

    def __init__(self, owner, name, fn, interface):
        self._owner, self._name, self._fn, self._interface = owner, name, fn, interface

    def __call__(self, *args, **kwargs):
        plain, outs = plain_arguments(args)
        result = self._fn(*plain, **{k: bare(v) for k, v in kwargs.items()})
        return self._owner._wrap_(give_back(result, outs))

    def __repr__(self):
        return (f"<SolidWorks method {self._interface}.{self._name} - a method, call it with parentheses: "
                f"{self._name}(...)>")

    __str__ = __repr__


class Auto:
    """A COM object whose members resolve by name over the type library's interfaces.

    _bound_: the early-bound instance the object came as (None for a raw dispatch);
    _oleobj_: its dispatch, so an Auto passes wherever a COM object is expected."""

    def __init__(self, obj, mod):
        if isinstance(obj, Auto):
            obj = obj._bound_ if obj._bound_ is not None else obj._oleobj_
        bound = obj if type(obj) in known(mod) else None
        d = self.__dict__
        d["_bound_"] = bound
        d["_oleobj_"] = getattr(obj, "_oleobj_", obj)
        d["_mod_"] = mod
        d["_faces_"] = {} if bound is None else {type(bound): bound}   # interface class -> instance
        d["_refused_"] = set()

    # --- resolving -----------------------------------------------------------------

    def _wrap_(self, value):
        return wrap(value, self._mod_)

    def _ask_(self, cls):
        """The object as interface `cls`, or None when it does not support it."""
        faces = self._faces_
        if cls in faces:
            return faces[cls]
        if cls in self._refused_:
            return None
        try:
            pointer = self._oleobj_.QueryInterface(cls.CLSID, _IID_IDISPATCH)
        except pythoncom.com_error as exc:
            if getattr(exc, "hresult", None) != E_NOINTERFACE:
                raise
            self._refused_.add(cls)
            return None
        except (AttributeError, TypeError):   # not a dispatch that can be asked: taken as it is
            pointer = self._oleobj_
        faces[cls] = cls(pointer)
        return faces[cls]

    def _face_(self, name: str):
        """The early-bound instance that has member `name`; None when the object supports no
        interface of the type library with that member (an add-in's object, or a wrong name)."""
        bound = self._bound_
        if bound is not None and has(type(bound), name):
            return bound
        for cls, instance in self._faces_.items():
            if has(cls, name):
                return instance
        for cls in index(self._mod_).get(name, ()):
            instance = self._ask_(cls)
            if instance is not None:
                return instance
        return None

    def _late_(self):
        """The object late-bound: for what the SolidWorks type library does not describe."""
        late = self.__dict__.get("_dyn_")
        if late is None:
            late = _late_bound(self._oleobj_)
            self.__dict__["_dyn_"] = late
        return late

    def _missing_(self, name: str, candidates) -> str:
        known = ", ".join(c.__name__ for c in self._faces_) or "no interface identified yet"
        if not candidates:
            return (f"No SolidWorks interface has a member '{name}' (this object: {known}). "
                    "api(obj, 'part of the name') lists what exists; api_search finds a member by name.")
        have = ", ".join(c.__name__ for c in candidates[:8])
        return (f"This object ({known}) has no member '{name}': it belongs to {have}, which the object "
                "does not support. Check what the object is (api(obj)) or how you got it.")

    def __getattr__(self, name: str):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        if name in DISPATCH_MEMBERS:
            return getattr(self._oleobj_, name)
        face = self._face_(name)
        if face is not None:
            value = getattr(face, name)
            if callable(value) and not is_com(value):
                return Member(self, name, value, type(face).__name__)
            return self._wrap_(value)
        # not a member of any SolidWorks interface this object supports: the object's own
        # name lookup decides (an add-in's API); a name it does not know either is the error
        try:
            value = getattr(self._late_(), name)
        except Exception:  # noqa: BLE001 - AttributeError, or the COM error of an unknown name
            raise AttributeError(self._missing_(name, index(self._mod_).get(name, ()))) from None
        if callable(value) and not is_com(value):
            return Member(self, name, value, "late-bound")
        return self._wrap_(value)

    def __setattr__(self, name: str, value):
        face = self._face_(name)
        if face is not None:
            setattr(face, name, bare(value))
            return
        try:
            setattr(self._late_(), name, bare(value))
        except Exception:  # noqa: BLE001
            raise AttributeError(self._missing_(name, index(self._mod_).get(name, ()))) from None

    # --- behaving like the object ---------------------------------------------------

    def __repr__(self):
        names = [c.__name__ for c in self._faces_]
        return f"<SolidWorks object {' / '.join(names) if names else '(interface found on first use)'}>"

    def __dir__(self):
        out = set()
        for cls in self._faces_:
            out |= members(cls)
        return sorted(out)

    def __eq__(self, other):
        if isinstance(other, Auto):
            return self._oleobj_ == other._oleobj_
        if other is None:
            return False
        return self._oleobj_ == getattr(other, "_oleobj_", other)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash(self._oleobj_)

    def __bool__(self):
        return True

    def __iter__(self):
        bound = self._bound_
        if bound is None or not hasattr(type(bound), "__iter__"):
            raise TypeError("This SolidWorks object is not a collection; call the method that lists its items.")
        return (self._wrap_(item) for item in bound)

    def __len__(self):
        bound = self._bound_
        if bound is None or not hasattr(type(bound), "__len__"):
            raise TypeError("This SolidWorks object has no length.")
        return len(bound)


def wrap(value, mod):
    """Any COM object in `value` - also inside tuples and lists - as Auto; the rest unchanged."""
    if value is None or isinstance(value, (str, bytes, int, float, bool, Auto)):
        return value
    if isinstance(value, tuple):
        return tuple(wrap(v, mod) for v in value)
    if isinstance(value, list):
        return [wrap(v, mod) for v in value]
    if _VARIANT is not None and isinstance(value, _VARIANT):
        return value
    if is_com(value):
        return Auto(value, mod)
    return value


def unwrap(value):
    """The early-bound instance (or raw dispatch) inside an Auto; anything else unchanged."""
    return bare(value)


def returning(fn, mod):
    """`fn` with its COM results wrapped as Auto and its Auto arguments unwrapped: for the
    script helpers (feature(), segments(), comp(), ...), so that what they hand out
    behaves like everything else in the script."""
    def call(*args, **kwargs):
        return wrap(fn(*[bare(a) for a in args], **{k: bare(v) for k, v in kwargs.items()}), mod)
    call.__name__ = getattr(fn, "__name__", "helper")
    call.__doc__ = getattr(fn, "__doc__", None)
    return call


def describe(obj, contains: str = "") -> dict:
    """api(obj) for an Auto: the interfaces identified so far and their members."""
    if not isinstance(obj, Auto):
        raise SolidWorksError("describe() takes an Auto object.")
    needle = contains.lower()
    if not obj._faces_:   # nothing used yet: ask the object for the interfaces scripts live on
        for name in PREFERRED:
            cls = getattr(obj._mod_, name, None)
            if isinstance(cls, type) and cls in known(obj._mod_):
                try:
                    obj._ask_(cls)
                except Exception:  # noqa: BLE001 - a description never fails a script
                    break
    faces = list(obj._faces_)
    methods, properties = set(), set()
    for cls in faces:
        props = set(getattr(cls, "_prop_map_get_", None) or ()) | set(getattr(cls, "_prop_map_put_", None) or ())
        properties |= props
        methods |= members(cls) - props
    pick = (lambda names: sorted(n for n in names if needle in n.lower()))
    note = None
    if not faces:
        note = ("The object answers to none of the common interfaces; its members still resolve by name "
                "when used. api_search finds a member by name.")
    return {"interface": " / ".join(c.__name__ for c in faces) or None, "methods": pick(methods),
            "properties": pick(properties), "note": note}
