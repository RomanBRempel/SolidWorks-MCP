"""A failed tool call leaves the part as it was.

Every MCP tool runs through SolidWorksSession.run_guarded: on a failure it
removes what the call added to the current part. Without it a refused call left
its sketch behind, which also counted in the bounding box.
"""

import asyncio

import pytest

pythoncom = pytest.importorskip("pythoncom")

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.session import SolidWorksSession

RPC_E_DISCONNECTED = -2147417848   # "the object invoked has disconnected from its clients"
E_NOINTERFACE = -2147467262        # "no such interface supported"


def disconnected(hresult=RPC_E_DISCONNECTED):
    return pythoncom.com_error(hresult, "the object invoked has disconnected", None, None)


class StillApp:
    """An ISldWorks link that stays alive."""

    def __init__(self):
        self.CommandInProgress = False


class FlakyApp:
    """An ISldWorks link that dies after `alive_reads` reads of CommandInProgress."""

    def __init__(self, alive_reads):
        self._alive = alive_reads

    @property
    def CommandInProgress(self):
        if self._alive > 0:
            self._alive -= 1
            return False
        raise disconnected()

    @CommandInProgress.setter
    def CommandInProgress(self, value):
        pass


class FlakyModel:
    """An IModelDoc2 that dies after `alive_reads` reads of its title."""

    def __init__(self, alive_reads):
        self._alive = alive_reads

    def GetTitle(self):
        if self._alive > 0:
            self._alive -= 1
            return "Part1"
        raise disconnected()


class RecordingSession(SolidWorksSession):
    """Records the guard's steps instead of touching SolidWorks."""

    def __init__(self, left=()):
        super().__init__()
        self.events = []
        self.left = list(left)

    def _history_snapshot(self):
        self.events.append("snapshot")
        return "Part1", {"Sketch1", "Base"}

    def _roll_back(self, snapshot):
        self.events.append(("roll back", snapshot))
        return self.left


def test_a_failed_call_is_rolled_back_to_the_snapshot_taken_before_it():
    session = RecordingSession()

    def fails():
        session.events.append("call")
        raise SolidWorksError("FeatureCut4 failed (None).")

    with pytest.raises(SolidWorksError, match="FeatureCut4"):
        session.run_guarded(fails)

    assert session.events == ["snapshot", "call", ("roll back", ("Part1", {"Sketch1", "Base"}))]


def test_a_successful_call_is_left_alone():
    session = RecordingSession()

    assert session.run_guarded(lambda: "done") == "done"
    assert session.events == ["snapshot"], "a call that worked must not be rolled back"


def test_what_could_not_be_removed_is_named_in_the_error():
    session = RecordingSession(left=["Sketch5"])

    def fails():
        raise SolidWorksError("Point is not on the +z face.")

    with pytest.raises(SolidWorksError, match="not on the .z face. Could not remove what the call added: Sketch5"):
        session.run_guarded(fails)


# --- losing the link to SolidWorks ---------------------------------------------
#
# The link dies in three distinct ways, and each must come back readable:
# SolidWorks itself gone (restarted or closed), only the current document gone
# (closed in SolidWorks or by a script), or only a sub-object gone (a face
# consumed by a rebuild) with the link itself fine.


def test_a_dead_link_found_before_the_call_reattaches():
    session = RecordingSession()
    session._sw = FlakyApp(alive_reads=0)
    reconnects = []
    session.connect = lambda: reconnects.append(True)

    assert session.run_guarded(lambda: "done") == "done"
    assert reconnects == [True]


def test_a_dead_current_document_found_before_the_call_is_dropped():
    session = RecordingSession()
    session._sw = StillApp()
    session._model = FlakyModel(alive_reads=0)

    assert session.run_guarded(lambda: "done") == "done"
    assert session._model is None, "the closed document must be dropped, not answered with a raw COM error"


def test_solidworks_restarted_during_the_call_reattaches_and_says_so():
    session = RecordingSession()
    session._sw = FlakyApp(alive_reads=2)  # alive for the probe and CommandInProgress, dead after
    reconnects = []
    session.connect = lambda: reconnects.append(True)

    def dies():
        raise disconnected()

    with pytest.raises(SolidWorksError, match="restarted during the call"):
        session.run_guarded(dies)
    assert reconnects == [True]
    assert session._model is None


def test_solidworks_closed_during_the_call_and_not_running_says_to_start_it():
    session = RecordingSession()
    session._sw = FlakyApp(alive_reads=2)

    def no_solidworks():
        raise SolidWorksError("No running SolidWorks found.")
    session.connect = no_solidworks

    def dies():
        raise disconnected(E_NOINTERFACE)

    with pytest.raises(SolidWorksError, match="not running"):
        session.run_guarded(dies)


def test_the_document_closed_during_the_call_is_dropped_and_named():
    session = RecordingSession()
    session._sw = StillApp()
    session._model = FlakyModel(alive_reads=1)  # alive for the probe, closed during the call

    def dies():
        raise disconnected()

    with pytest.raises(SolidWorksError, match="document was closed during the call"):
        session.run_guarded(dies)
    assert session._model is None


def test_a_dead_sub_object_with_the_link_fine_rolls_back_and_keeps_the_error():
    session = RecordingSession()
    session._sw = StillApp()
    session._model = FlakyModel(alive_reads=99)

    def dies():
        raise disconnected()

    with pytest.raises(pythoncom.com_error):
        session.run_guarded(dies)
    assert ("roll back", ("Part1", {"Sketch1", "Base"})) in session.events
    assert session._model is not None, "the document is alive; it must stay current"


@pytest.mark.solidworks
def test_a_failed_tool_call_through_the_server_leaves_the_part_as_it_was():
    """End to end, the way an MCP client calls: a pocket drawn beside the block
    fails only after its sketch is complete, which then stayed in the tree and
    stretched the bounding box to x = 110. (A sketch left empty SolidWorks drops
    by itself, so that case proves nothing.)"""
    from solidworks_mcp import server

    def call(tool, *args):
        return asyncio.run(tool(*args))

    call(server.new_part)
    try:
        block = call(server.add_box, 40, 20, 10)
        before = call(server.list_features)["features"]

        refused = call(server.cut_profile, [[100, 100], [110, 100], [110, 110], [100, 110]], 2)

        assert refused["ok"] is False, refused
        assert call(server.list_features)["features"] == before, "the failed call left its sketch in the tree"
        assert call(server.get_bounding_box)["bounding_box_mm"] == block["mass_properties"]["bounding_box_mm"]
    finally:
        call(server.close_part)
