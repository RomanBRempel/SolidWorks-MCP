"""typed.Auto: members of a COM object resolve by name over the type library's interfaces.

Stand-in interface classes and dispatches: no SolidWorks, no pywin32 needed.
"""

import types

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.rbr import typed


class _NoInterface(Exception):
    hresult = typed.E_NOINTERFACE


class PyIDispatch:  # the name is_com knows a bare dispatch by
    """A raw dispatch that supports the interfaces named in `supports` (by their CLSID)."""

    def __init__(self, *supports, data=None):
        self.supports = set(supports)
        self.asked = []
        self.data = data or {}

    def QueryInterface(self, iid, _as=None):
        self.asked.append(iid)
        if iid not in self.supports:
            raise _NoInterface()
        return self


_Dispatch = PyIDispatch


class _Face:
    """Base of the stand-in interface classes: holds the dispatch like a makepy class."""

    CLSID = None
    _prop_map_get_ = {}
    _prop_map_put_ = {}

    def __init__(self, oleobj):
        self.__dict__["_oleobj_"] = oleobj

    def __getattr__(self, name):
        if name in type(self)._prop_map_get_:
            return self._oleobj_.data[name]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name in type(self)._prop_map_put_:
            self._oleobj_.data[name] = value
        else:
            raise AttributeError(name)


class IModelDoc(_Face):
    CLSID = "doc1"

    def GetTitle(self):
        return "old"


class IModelDoc2(_Face):
    CLSID = "doc2"
    _prop_map_get_ = {"Extension": None, "Visible": None}
    _prop_map_put_ = {"Visible": None}

    def GetTitle(self):
        return "Part1"

    def Save3(self, options, errors, warnings):
        return (True, 0, 4)

    def FirstFeature(self):
        return _Dispatch("feat")

    def Take(self, thing, things):
        return (type(thing).__name__, [type(t).__name__ for t in things])


class IPartDoc(_Face):
    CLSID = "part"

    def GetBodies2(self, kind, visible):
        return (_Dispatch("body"), _Dispatch("body"))


class IFeature(_Face):
    CLSID = "feat"
    _prop_map_get_ = {"Name": None}
    _prop_map_put_ = {"Name": None}

    def GetTypeName2(self):
        return "Extrusion"

    def Select2(self, append, mark):
        return True


class IBody2(_Face):
    CLSID = "body"

    def GetFaces(self):
        return None


class IEntity(_Face):
    CLSID = "entity"

    def Select2(self, append, mark):
        return "entity"


CLASSES = [IModelDoc, IModelDoc2, IPartDoc, IFeature, IBody2, IEntity]


@pytest.fixture
def mod(monkeypatch):
    module = types.SimpleNamespace(**{c.__name__: c for c in CLASSES})
    typed.index(module, CLASSES)
    monkeypatch.setattr(typed.pythoncom, "com_error", _NoInterface, raising=False)
    yield module
    typed._INDEX.pop(id(module), None)
    typed._KNOWN.pop(id(module), None)


def test_the_index_lists_each_member_under_every_interface_that_has_it(mod):
    table = typed.index(mod)
    assert table["GetTitle"] == (IModelDoc2, IModelDoc)          # preferred, and the newest first
    assert table["Select2"] == (IFeature, IEntity)                # both preferred: in the preferred order
    assert table["Name"] == (IFeature,) and table["Visible"] == (IModelDoc2,)
    assert "CLSID" not in table and "_prop_map_get_" not in table


def test_ranked_puts_the_newest_of_a_family_first():
    class IThing(_Face):
        pass

    class IThing3(_Face):
        pass

    class IThing2(_Face):
        pass

    assert typed.ranked([IThing, IThing2, IThing3]) == [IThing3, IThing2, IThing]


def test_a_raw_dispatch_gets_its_methods_as_methods(mod):
    doc = typed.Auto(_Dispatch("doc2", "doc1", "part"), mod)
    title = doc.GetTitle
    assert isinstance(title, typed.Member)                         # never run by merely naming it
    assert "call it with parentheses" in str(title)
    assert title() == "Part1"                                      # IModelDoc2, not the older IModelDoc
    assert "IModelDoc2" in repr(doc)


def test_only_interfaces_that_have_the_member_are_asked_and_each_once(mod):
    raw = _Dispatch("doc2", "part")
    doc = typed.Auto(raw, mod)
    doc.GetTitle()
    doc.GetTitle()
    assert raw.asked == ["doc2"]
    assert len(doc.GetBodies2(0, True)) == 2
    assert raw.asked == ["doc2", "part"]


def test_a_member_of_another_interface_is_found_on_the_same_object(mod):
    model = typed.Auto(IModelDoc2(_Dispatch("doc2", "part")), mod)   # came early-bound as IModelDoc2
    assert model._bound_ is not None
    bodies = model.GetBodies2(0, True)                                # an IPartDoc member
    assert all(isinstance(b, typed.Auto) for b in bodies)


def test_what_a_call_returns_is_wrapped_too_also_inside_tuples(mod):
    doc = typed.Auto(_Dispatch("doc2"), mod)
    feat = doc.FirstFeature()
    assert isinstance(feat, typed.Auto) and feat.GetTypeName2() == "Extrusion"
    assert doc.Save3(1, 0, 0) == (True, 0, 4)                         # plain values stay plain


def test_properties_read_and_write_through(mod):
    raw = _Dispatch("feat", data={"Name": "Cut1"})
    feat = typed.Auto(raw, mod)
    assert feat.Name == "Cut1"
    feat.Name = "Pocket"
    assert raw.data["Name"] == "Pocket"


def test_a_property_holding_a_com_object_comes_wrapped(mod):
    extension = _Dispatch("ext")
    doc = typed.Auto(_Dispatch("doc2", data={"Extension": extension}), mod)
    assert isinstance(doc.Extension, typed.Auto) and doc.Extension._oleobj_ is extension


def test_an_unsupported_member_names_the_interfaces_that_have_it(mod):
    feat = typed.Auto(_Dispatch("feat"), mod)
    feat.GetTypeName2()
    with pytest.raises(AttributeError, match="IPartDoc"):
        feat.GetBodies2(0, True)
    with pytest.raises(AttributeError, match="No SolidWorks interface has a member 'Nope'"):
        feat.Nope


def test_an_object_that_answers_no_interface_id_is_bound_as_the_likeliest(mod):
    doc = typed.Auto(_Dispatch(), mod)      # QueryInterface refuses everything
    assert doc.GetTitle() == "Part1"         # the first candidate, on the dispatch as it is


def test_auto_arguments_are_unwrapped_also_inside_lists(mod):
    doc = typed.Auto(_Dispatch("doc2"), mod)
    feat = typed.Auto(IFeature(_Dispatch("feat")), mod)
    raw = typed.Auto(_Dispatch("body"), mod)
    assert doc.Take(feat, [feat, raw]) == ("IFeature", ["IFeature", "PyIDispatch"])


def test_equality_and_hash_follow_the_dispatch(mod):
    raw = _Dispatch("feat")
    a, b = typed.Auto(raw, mod), typed.Auto(IFeature(raw), mod)
    assert a == b and hash(a) == hash(b) and a != typed.Auto(_Dispatch("feat"), mod)
    assert a != None and bool(a)  # noqa: E711 - the comparison is what is tested


def test_wrap_leaves_plain_values_and_wraps_com_objects(mod):
    raw = _Dispatch("feat")

    class _Dyn:
        _oleobj_ = raw

    out = typed.wrap((1, "a", None, [_Dyn(), 2.5], {"k": 1}), mod)
    assert out[:3] == (1, "a", None) and out[4] == {"k": 1}
    assert isinstance(out[3][0], typed.Auto) and out[3][0]._oleobj_ is raw and out[3][1] == 2.5
    auto = typed.Auto(raw, mod)
    assert typed.wrap(auto, mod) is auto and typed.unwrap(auto) is raw
    assert typed.unwrap(typed.Auto(IFeature(raw), mod)).__class__ is IFeature


def test_returning_dresses_a_helper(mod):
    seen = []

    def helper(obj, items=()):
        seen.append((obj, list(items)))
        return [IFeature(_Dispatch("feat")), "text"]

    dressed = typed.returning(helper, mod)
    raw = _Dispatch("feat")
    out = dressed(typed.Auto(raw, mod), items=[typed.Auto(raw, mod)])
    assert seen == [(raw, [raw])]
    assert isinstance(out[0], typed.Auto) and out[1] == "text" and dressed.__name__ == "helper"


class _Variant:
    def __init__(self, varianttype, value):
        self.varianttype, self.value = varianttype, value


def test_by_reference_variants_are_filled_and_the_return_value_comes_alone(mod, monkeypatch):
    monkeypatch.setattr(typed, "_VARIANT", _Variant)
    errors, warnings = _Variant(typed._VT_BYREF | 3, 0), _Variant(typed._VT_BYREF | 3, 0)
    doc = typed.Auto(_Dispatch("doc2"), mod)
    assert doc.Save3(1, errors, warnings) is True          # the late-bound convention
    assert (errors.value, warnings.value) == (0, 4)
    assert doc.Save3(1, 0, 0) == (True, 0, 4)               # the early-bound one, untouched


def test_plain_arguments_and_give_back_are_pure(monkeypatch):
    monkeypatch.setattr(typed, "_VARIANT", _Variant)
    out = _Variant(typed._VT_BYREF | 3, 7)
    by_value = _Variant(3, 9)                               # a VARIANT that is not by reference stays
    plain, outs = typed.plain_arguments(("a", out, by_value))
    assert plain == ("a", 7, by_value) and outs == [out]
    assert typed.give_back(("ret", 5), outs) == "ret" and out.value == 5
    assert typed.give_back("ret", outs) == "ret"            # nothing came back: nothing to fill
    assert typed.give_back(("ret", 5), []) == ("ret", 5)


def test_describe_lists_the_interfaces_and_members(mod):
    doc = typed.Auto(_Dispatch("doc2", "part"), mod)
    report = typed.describe(doc, "get")                    # nothing used yet: the common ones are asked
    assert report["interface"] == "IModelDoc2 / IPartDoc"
    assert report["methods"] == ["GetBodies2", "GetTitle"] and report["note"] is None
    assert typed.describe(typed.Auto(_Dispatch(), mod))["note"]
    with pytest.raises(SolidWorksError):
        typed.describe(object())
