"""query: directions, counts, boxes and the reference store on disk; apiref: ranking the search (pure)."""

import base64
import hashlib
import os
import re

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.rbr import apiref, query


# --- unit, direction, parallel_spec ---------------------------------------------------------

def test_unit_scales_a_vector_to_length_one():
    assert query.unit([3, 4, 0]) == pytest.approx((0.6, 0.8, 0.0))
    assert query.unit((0, 0, -7)) == (0.0, 0.0, -1.0)
    assert isinstance(query.unit([1, 0, 0]), tuple)


def test_unit_refuses_a_zero_vector():
    with pytest.raises(SolidWorksError, match="non-zero vector"):
        query.unit([0, 0, 0])


@pytest.mark.parametrize("value, expected", [
    ("+z", (0.0, 0.0, 1.0)), ("-x", (-1.0, 0.0, 0.0)), ("y", (0.0, 1.0, 0.0)), (" -Y ", (0.0, -1.0, 0.0)),
    ("Z", (0.0, 0.0, 1.0)), ([0, 0, 2], (0.0, 0.0, 1.0)), ((0, -3, 4), (0.0, -0.6, 0.8))])
def test_direction_from_an_axis_name_or_a_vector(value, expected):
    assert query.direction(value) == pytest.approx(expected)


@pytest.mark.parametrize("value", ["w", "", "+", "xy", "front"])
def test_direction_refuses_an_unknown_axis(value):
    with pytest.raises(SolidWorksError, match="A direction is"):
        query.direction(value)


def test_direction_refuses_a_zero_vector():
    with pytest.raises(SolidWorksError, match="non-zero"):
        query.direction([0, 0, 0])


def test_parallel_to_a_named_plane_or_to_a_line():
    assert query.parallel_spec("front") == ("plane", (0.0, 0.0, 1.0))
    assert query.parallel_spec(" Top ") == ("plane", (0.0, 1.0, 0.0))
    assert query.parallel_spec("RIGHT") == ("plane", (1.0, 0.0, 0.0))
    assert query.parallel_spec("x") == ("line", (1.0, 0.0, 0.0))
    assert query.parallel_spec("-z") == ("line", (0.0, 0.0, -1.0))
    kind, vector = query.parallel_spec([0, 5, 0])
    assert kind == "line" and vector == pytest.approx((0.0, 1.0, 0.0))
    with pytest.raises(SolidWorksError):
        query.parallel_spec("side")


# --- count_ok -------------------------------------------------------------------------------

def test_count_ok_without_an_expectation_or_when_it_is_met():
    assert query.count_ok(None, 0) is None and query.count_ok(None, 99) is None
    assert query.count_ok(2, 2) is None and query.count_ok("2", 2) is None and query.count_ok(0, 0) is None
    assert query.count_ok([1, 3], 1) is None and query.count_ok((1, 3), 3) is None


def test_count_ok_says_what_was_expected():
    assert query.count_ok(1, 2) == "1 was expected"
    assert query.count_ok(3, 2) == "3 were expected"
    assert query.count_ok(0, 2) == "0 were expected"
    assert query.count_ok([1, 3], 4) == "1 to 3 were expected"
    assert query.count_ok([2, 5], 1) == "2 to 5 were expected"


@pytest.mark.parametrize("expect", ["two", [1, 2, 3], [1], {"min": 1}])
def test_count_ok_refuses_what_is_no_count(expect):
    with pytest.raises(SolidWorksError, match=r"expect is a count or \[min, max\]"):
        query.count_ok(expect, 1)


@pytest.mark.parametrize("expect", [["a", "b"], [1, None], [None, 3], ["1", "many"]])
def test_count_ok_refuses_a_range_whose_ends_are_no_counts(expect):
    with pytest.raises(SolidWorksError, match=r"expect is a count or \[min, max\]"):
        query.count_ok(expect, 1)


# --- in_box, in_range -----------------------------------------------------------------------

def test_in_box_takes_metres_and_two_mm_corners_in_any_order():
    box = (0.001, 0.002, 0.003, 0.004, 0.005, 0.006)
    assert query.in_box(box, [[0, 0, 0], [10, 10, 10]])
    assert query.in_box(box, [[10, 10, 10], [0, 0, 0]])
    assert query.in_box(box, [[10, 0, 10], [0, 10, 0]])
    assert query.in_box(box, [[1, 2, 3], [4, 5, 6]])  # the box itself: the faces count as inside


def test_in_box_is_false_when_the_entity_sticks_out_anywhere():
    box = (0.001, 0.002, 0.003, 0.004, 0.005, 0.006)
    assert not query.in_box(box, [[0, 0, 0], [10, 10, 5.9]])     # out at the top
    assert not query.in_box(box, [[1.1, 0, 0], [10, 10, 10]])    # out at the low x side
    assert not query.in_box(box, [[20, 20, 20], [30, 30, 30]])   # elsewhere
    assert query.in_box(box, [[1.00005, 0, 0], [10, 10, 10]])    # 0.05 um: numeric noise


def test_in_range_with_a_number_bounds_or_nothing():
    assert query.in_range(12.3, None)
    assert query.in_range(5.0, 5) and query.in_range(5.0005, 5) and not query.in_range(5.01, 5)
    assert query.in_range(50000.4, 50000) and not query.in_range(50006.0, 50000)  # 1e-4 of a large value
    assert query.in_range(5.0, [4, 6]) and query.in_range(4.0, [4, 6]) and query.in_range(6.0, (4, 6))
    assert not query.in_range(3.9, [4, 6]) and not query.in_range(6.1, [4, 6])


def test_in_range_open_ended():
    assert query.in_range(100.0, [4, None]) and not query.in_range(3.0, [4, None])
    assert query.in_range(-100.0, [None, 6]) and not query.in_range(7.0, [None, 6])
    assert query.in_range(1e9, [None, None])


# --- is_ref, ref_tokens, short_of -----------------------------------------------------------

def test_is_ref_knows_the_prefix_only_on_text():
    assert query.is_ref("ref:0123456789ab") and query.is_ref("  REF:0123456789ab") and query.is_ref("ref:")
    for token in ("#5", "+z", "edge:5", "", "reference", "xref:12", 5, None, ["ref:0123456789ab"], b"ref:12"):
        assert not query.is_ref(token)


def test_ref_tokens_from_one_token_a_string_of_them_or_a_list():
    assert query.ref_tokens("ref:aa") == ["ref:aa"]
    assert query.ref_tokens("ref:aa, ref:bb") == ["ref:aa", "ref:bb"]
    assert query.ref_tokens("ref:aa ref:bb,ref:cc") == ["ref:aa", "ref:bb", "ref:cc"]
    assert query.ref_tokens(["ref:aa", " ref:bb "]) == ["ref:aa", "ref:bb"]
    assert query.ref_tokens(("ref:aa",)) == ["ref:aa"]


@pytest.mark.parametrize("selector", ["2,5", "#5", "ref:aa, 5", ["ref:aa", "#5"], [3, 4], "", [], 5, None])
def test_ref_tokens_is_none_for_any_other_selector(selector):
    assert query.ref_tokens(selector) is None


def test_short_name_is_the_start_of_the_sha1():
    ref = bytes(range(200))
    assert query.short_of(ref) == hashlib.sha1(ref).hexdigest()[:12]
    assert query.short_of(bytearray(ref)) == query.short_of(ref)
    assert query.short_of(ref) != query.short_of(ref + b"\x00")
    assert re.fullmatch(r"[0-9a-f]{12}", query.short_of(b""))


# --- the reference store --------------------------------------------------------------------

@pytest.fixture
def store(swmcp_home, monkeypatch):
    """An empty store in a temporary home; the module's state is put back afterwards."""
    monkeypatch.setattr(query, "_store", {})
    monkeypatch.setattr(query, "_unsaved", [])
    monkeypatch.setattr(query, "_store_seen", 0)
    monkeypatch.setattr(query, "_store_tail", b"")
    monkeypatch.setattr(query, "_on_disk", set())
    monkeypatch.setattr(query, "_pending", set())
    return os.path.join(swmcp_home, "refs.jsonl")


def ref(k: int) -> bytes:
    """A stand-in for a persistent reference: 60 bytes, different for every k."""
    return hashlib.sha256(str(k).encode()).digest() + bytes([k % 256]) * 28


def other_process(monkeypatch, store=None, seen=0, tail=None):
    """Module state as another server process has it: its own memory, the same file."""
    monkeypatch.setattr(query, "_store", dict(store or {}))
    monkeypatch.setattr(query, "_unsaved", [])
    monkeypatch.setattr(query, "_store_seen", seen)
    # what that process read up to its offset, as the file is at this moment: the tail it
    # remembers (a later pruning shows against it) and the names it knows to be on disk
    try:
        with open(query._store_path(), "rb") as fh:
            head = fh.read(seen)
    except OSError:
        head = b""
    monkeypatch.setattr(query, "_store_tail", head[-query.TAIL_BYTES:] if tail is None else tail)
    monkeypatch.setattr(query, "_on_disk", {line.split(b" ")[0].decode("ascii") for line in head.splitlines()})
    monkeypatch.setattr(query, "_pending", set())


def lines_of(path):
    with open(path, "rb") as fh:
        return fh.read().splitlines()


def test_token_is_the_prefix_and_twelve_hex_digits(store):
    token = query.to_token(ref(1))
    assert re.fullmatch(r"ref:[0-9a-f]{12}", token)
    assert token == "ref:" + query.short_of(ref(1))
    assert query.is_ref(token)


def test_same_bytes_give_the_same_token_and_other_bytes_another(store):
    assert query.to_token(ref(1)) == query.to_token(bytes(ref(1))) == query.to_token(bytearray(ref(1)))
    assert query.to_token(ref(1)) != query.to_token(ref(2))


def test_token_resolves_to_its_bytes_before_any_flush(store):
    token = query.to_token(ref(1))
    assert query.from_token(token) == ref(1)
    assert query.from_token("  " + token.upper() + " ") == ref(1)  # case and outer spaces do not matter
    assert not os.path.exists(store)


def test_flush_writes_one_line_per_reference(store):
    tokens = [query.to_token(ref(k)) for k in range(3)]
    query.to_token(ref(0))  # known already: not written twice
    query.flush()
    lines = lines_of(store)
    assert len(lines) == 3
    for token, line, k in zip(tokens, lines, range(3)):
        name, _, body = line.partition(b" ")
        assert "ref:" + name.decode("ascii") == token and base64.b64decode(body) == ref(k)
    assert os.path.dirname(store) == os.environ["SWMCP_HOME"]


def test_flush_without_new_references_writes_nothing(store):
    query.flush()
    assert not os.path.exists(store)
    query.to_token(ref(1))
    query.flush()
    size = os.path.getsize(store)
    query.flush()
    query.to_token(ref(1))
    query.flush()
    assert os.path.getsize(store) == size


def test_another_process_reads_the_reference_back_from_the_file(store, monkeypatch):
    token = query.to_token(ref(7))
    query.flush()
    other_process(monkeypatch)
    assert query.from_token(token) == ref(7)


def test_references_appended_later_are_taken_in_too(store, monkeypatch):
    first = query.to_token(ref(1))
    query.flush()
    writer = dict(query._store)
    other_process(monkeypatch)
    assert query.from_token(first) == ref(1)
    reader, seen, tail = dict(query._store), query._store_seen, query._store_tail
    other_process(monkeypatch, writer)       # the first process hands out one more
    second = query.to_token(ref(2))
    query.flush()
    other_process(monkeypatch, reader, seen, tail)  # the reader has seen only the first line
    assert query.from_token(second) == ref(2)


def test_unflushed_reference_is_unknown_to_another_process(store, monkeypatch):
    token = query.to_token(ref(7))
    other_process(monkeypatch)
    with pytest.raises(SolidWorksError, match="No reference"):
        query.from_token(token)


def test_unknown_short_token_is_refused(store):
    query.to_token(ref(1))
    query.flush()
    with pytest.raises(SolidWorksError, match="No reference 'ref:0123456789ab' is known here"):
        query.from_token("ref:0123456789AB")
    with pytest.raises(SolidWorksError, match="not a valid entity reference"):
        query.from_token("ref:")   # no name at all: not a reference, rather than an unknown one


def test_long_base64_form_is_still_accepted(store):
    data = b"".join(ref(k) for k in range(7))  # 420 bytes, as a real reference
    text = base64.urlsafe_b64encode(data).decode("ascii")
    assert query.from_token("ref:" + text) == data
    assert query.from_token("ref:" + text.rstrip("=")) == data      # padding left off
    odd = data[:-1]                                                 # a length that needs padding
    assert query.from_token("REF:" + base64.urlsafe_b64encode(odd).decode("ascii").rstrip("=")) == odd
    assert query._store == {} and not os.path.exists(store)         # nothing to remember: the token is the bytes


def test_long_form_that_is_not_base64_is_refused(store):
    with pytest.raises(SolidWorksError, match="not a valid entity reference"):
        query.from_token("ref:" + "x" * 25)  # no base64 text has 4n + 1 characters


@pytest.mark.parametrize("token", ["#5", "+z", "edge:5", "2,5", "", "0123456789ab", 5, None])
def test_what_is_no_reference_token_gives_none(store, token):
    assert query.from_token(token) is None


def test_damaged_line_in_the_file_names_nothing(store, monkeypatch):
    good = query.to_token(ref(1))
    query.flush()
    with open(store, "ab") as fh:
        fh.write(b"0123456789ab !!not base64!!\n")
    after = query.to_token(ref(2))
    query.flush()
    other_process(monkeypatch)
    assert query.from_token(good) == ref(1) and query.from_token(after) == ref(2)


def test_line_still_being_written_is_left_for_the_next_look(store, monkeypatch):
    token = query.to_token(ref(1))
    query.flush()
    late = query.short_of(ref(2))
    half = f"{late} {base64.b64encode(ref(2)).decode('ascii')}"
    with open(store, "ab") as fh:
        fh.write(half[:30].encode("ascii"))  # another process is in the middle of its write
    other_process(monkeypatch)
    assert query.from_token(token) == ref(1)
    with pytest.raises(SolidWorksError, match="No reference"):
        query.from_token("ref:" + late)
    with open(store, "ab") as fh:
        fh.write((half[30:] + "\n").encode("ascii"))
    assert query.from_token("ref:" + late) == ref(2)


def test_full_store_keeps_the_newer_half(store, monkeypatch):
    old = [query.to_token(ref(k)) for k in range(10)]
    query.flush()
    assert len(lines_of(store)) == 10
    monkeypatch.setattr(query, "STORE_MAX_BYTES", 200)  # the ten lines are far past it
    new = [query.to_token(ref(k)) for k in (100, 101)]
    query.flush()
    names = [line.split(b" ")[0].decode("ascii") for line in lines_of(store)]
    assert ["ref:" + n for n in names] == old[5:] + new
    assert not os.path.exists(store + ".tmp")
    # this process still knows every reference it handed out
    assert [query.from_token(t) for t in old + new] == [ref(k) for k in (*range(10), 100, 101)]
    # a fresh process: the newer half and the new ones resolve, the older half is gone
    other_process(monkeypatch)
    assert [query.from_token(t) for t in new] == [ref(100), ref(101)]
    assert [query.from_token(t) for t in old[5:]] == [ref(k) for k in range(5, 10)]
    for token in old[:5]:
        with pytest.raises(SolidWorksError, match="No reference"):
            query.from_token(token)


def test_token_handed_out_again_after_its_line_was_pruned_is_stored_again(store, monkeypatch):
    old = [query.to_token(ref(k)) for k in range(10)]
    query.flush()
    monkeypatch.setattr(query, "STORE_MAX_BYTES", 200)
    query.to_token(ref(100))
    query.flush()                           # the store drops its older half: lines 0..4
    again = query.to_token(ref(0))          # the same face is found once more and its token goes out in a reply
    query.flush()
    assert again == old[0] and query.from_token(again) == ref(0)
    other_process(monkeypatch)              # the next session
    assert query.from_token(again) == ref(0)


def test_store_below_the_limit_is_not_pruned(store, monkeypatch):
    for k in range(4):
        query.to_token(ref(k))
    query.flush()
    monkeypatch.setattr(query, "STORE_MAX_BYTES", os.path.getsize(store))  # exactly at the limit: not past it
    query.to_token(ref(4))
    query.flush()
    assert len(lines_of(store)) == 5


def test_process_that_read_a_file_since_pruned_to_a_smaller_one_reads_it_again(store, monkeypatch):
    old = [query.to_token(ref(k)) for k in range(10)]
    query.flush()
    writer = dict(query._store)
    other_process(monkeypatch)
    assert query.from_token(old[9]) == ref(9)
    reader, seen, tail = dict(query._store), query._store_seen, query._store_tail
    assert seen == os.path.getsize(store)
    other_process(monkeypatch, writer)
    monkeypatch.setattr(query, "STORE_MAX_BYTES", 200)
    new = query.to_token(ref(100))
    query.flush()                                   # 10 lines -> 5 + 1: smaller than what the reader saw
    assert os.path.getsize(store) < seen
    other_process(monkeypatch, reader, seen, tail)
    assert query.from_token(new) == ref(100)


def test_process_whose_offset_lies_inside_a_pruned_file_finds_what_the_file_holds(store, monkeypatch):
    """The reader saw 10 lines; the writer grew the file to 30 and pruned it to 16. The file
    is still longer than what the reader saw, so only its size does not show the pruning."""
    tokens = [query.to_token(ref(k)) for k in range(10)]
    query.flush()
    writer = dict(query._store)
    other_process(monkeypatch)
    assert query.from_token(tokens[0]) == ref(0)
    reader, seen, tail = dict(query._store), query._store_seen, query._store_tail
    other_process(monkeypatch, writer)
    tokens += [query.to_token(ref(k)) for k in range(10, 30)]
    query.flush()
    monkeypatch.setattr(query, "STORE_MAX_BYTES", 200)
    tokens.append(query.to_token(ref(30)))
    query.flush()                                   # keeps lines 15..29 and adds line 30
    names = [line.split(b" ")[0].decode("ascii") for line in lines_of(store)]
    assert ["ref:" + n for n in names] == tokens[15:]
    assert os.path.getsize(store) > seen
    other_process(monkeypatch, reader, seen, tail)
    assert query.from_token(tokens[30]) == ref(30)  # appended after the reader's offset
    assert query.from_token(tokens[20]) == ref(20)  # in the file, before the reader's offset


# --- apiref.rank ----------------------------------------------------------------------------

def row(interface, member, kind="method", signature="()", doc="", **extra):
    return {"interface": interface, "member": member, "kind": kind, "signature": signature, "doc": doc, **extra}


ROWS = [
    row("IPartDoc", "FeatureByName", doc="Gets a feature by its name; see also EditFreeze."),
    row("swMoveFreezeBarTo_e", "swMoveFreezeBarToTop", kind="constant", signature="= 4", value=4),
    row("IFeatureManager", "InsertCutSwept5", doc="Inserts a swept cut."),
    row("IFeatureManager", "EditFreeze", doc="Moves the freeze bar."),
    row("IFeatureManager", "InsertCutSwept", doc="Obsolete."),
    row("IFeatureManager", "EditFreezeAll", doc=""),
    row("IModelDoc2", "PreEditFreeze", doc=""),
    row("IFeature", "Name", kind="property", signature="get/set"),
    row("IBody2", "Name", kind="property", signature="get/set"),
]


def names(rows):
    return [f"{r['interface']}.{r['member']}" for r in rows]


def test_rank_exact_name_then_prefix_then_inside_then_help_text():
    assert names(apiref.rank(ROWS, "EditFreeze")) == [
        "IFeatureManager.EditFreeze", "IFeatureManager.EditFreezeAll", "IModelDoc2.PreEditFreeze",
        "IPartDoc.FeatureByName"]


def test_rank_is_case_blind():
    assert names(apiref.rank(ROWS, "editfreeze")) == names(apiref.rank(ROWS, "EDITFREEZE"))
    assert names(apiref.rank(ROWS, "insertcutswept"))[0] == "IFeatureManager.InsertCutSwept"


def test_rank_shorter_names_first_within_one_class():
    assert names(apiref.rank(ROWS, "Insert")) == ["IFeatureManager.InsertCutSwept", "IFeatureManager.InsertCutSwept5"]
    assert names(apiref.rank(ROWS, "name", kind="property")) == ["IBody2.Name", "IFeature.Name"]  # then by interface


def test_rank_needs_every_word_somewhere():
    found = names(apiref.rank(ROWS, "freeze bar"))
    assert found == ["swMoveFreezeBarTo_e.swMoveFreezeBarToTop", "IFeatureManager.EditFreeze"]
    assert apiref.rank(ROWS, "freeze nonsense") == []
    assert names(apiref.rank(ROWS, "swept, cut")) == ["IFeatureManager.InsertCutSwept",
                                                      "IFeatureManager.InsertCutSwept5"]


def test_rank_words_may_match_the_interface_name():
    assert names(apiref.rank(ROWS, "featuremanager freeze")) == ["IFeatureManager.EditFreeze",
                                                                 "IFeatureManager.EditFreezeAll"]


def test_rank_by_kind():
    assert names(apiref.rank(ROWS, "freeze", kind="constant")) == ["swMoveFreezeBarTo_e.swMoveFreezeBarToTop"]
    assert all(r["kind"] == "method" for r in apiref.rank(ROWS, "freeze", kind="method"))
    assert names(apiref.rank(ROWS, "name", kind="Property")) == ["IBody2.Name", "IFeature.Name"]
    assert names(apiref.rank(ROWS, "name", kind=None))[:2] == ["IBody2.Name", "IFeature.Name"]  # None: any


def test_rank_interface_dot_member_narrows_to_one_interface():
    assert names(apiref.rank(ROWS, "IFeatureManager.EditFreeze")) == ["IFeatureManager.EditFreeze",
                                                                      "IFeatureManager.EditFreezeAll"]
    assert names(apiref.rank(ROWS, "FeatureManager.EditFreeze"))[0] == "IFeatureManager.EditFreeze"  # without the I
    assert names(apiref.rank(ROWS, "ibody2.name")) == ["IBody2.Name"]
    assert names(apiref.rank(ROWS, "IPartDoc.Name")) == ["IPartDoc.FeatureByName"]  # the name contains the word
    assert apiref.rank(ROWS, "IPartDoc.Swept") == []  # other interfaces have it, this one does not
    assert names(apiref.rank(ROWS, "IModelDoc2.EditFreeze")) == ["IModelDoc2.PreEditFreeze"]


def test_rank_interface_argument():
    assert names(apiref.rank(ROWS, "name", interface="IFeature")) == ["IFeature.Name"]
    assert names(apiref.rank(ROWS, "", interface="IModelDoc2")) == ["IModelDoc2.PreEditFreeze"]  # all its members
    assert len(apiref.rank(ROWS, "", interface="featuremanager")) == 4


def test_rank_limit():
    assert len(apiref.rank(ROWS, "freeze", limit=2)) == 2
    assert names(apiref.rank(ROWS, "freeze", limit=1)) == ["IFeatureManager.EditFreeze"]
    assert len(apiref.rank(ROWS, "freeze", limit=0)) == 1  # at least one
    assert len(apiref.rank(ROWS, "freeze")) == 5


def test_rank_returns_the_rows_themselves():
    (found,) = apiref.rank(ROWS, "swMoveFreezeBarToTop")
    assert found is ROWS[1] and found["value"] == 4


@pytest.mark.parametrize("query_text", ["", "   ", None, " , "])
def test_rank_without_words_is_refused(query_text):
    with pytest.raises(SolidWorksError, match="Give a name or words"):
        apiref.rank(ROWS, query_text)


def test_rank_unknown_kind_is_refused():
    with pytest.raises(SolidWorksError, match="kind is 'any', 'method', 'property' or 'constant'"):
        apiref.rank(ROWS, "freeze", kind="enum")
