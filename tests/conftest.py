"""Shared pytest fixtures.

Two layers of tests live under tests/:
- pure unit tests (no SolidWorks) -- always run, CI-friendly;
- integration tests marked `solidworks` -- they use the `part` fixture below,
  which connects to a running SolidWorks and SKIPS the whole suite if it can't.

Run only the fast layer with:  pytest -m "not solidworks"
"""

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.session import SolidWorksSession


@pytest.fixture(scope="session")
def sw():
    """A connected SolidWorksSession shared across integration tests.

    Skips the integration suite when SolidWorks is not reachable.
    """
    session = SolidWorksSession()
    try:
        session.connect()
    except SolidWorksError as exc:
        pytest.skip(f"SolidWorks not reachable: {exc}")
    before = session._command_in_progress(True)  # as within every tool call the server makes
    yield session
    session._command_in_progress(before)


GDI_STOP_PCT = 75.0   # every document window keeps about 40 GDI objects for good after closing


@pytest.fixture(autouse=True)
def _stop_before_solidworks_runs_out_of_gdi(request):
    """Stop the run before SolidWorks runs out of GDI objects. A test that opens a window
    leaves about 40 behind for good (+3400 in 77 assembly tests, measured 2026-11-23); about
    two thousand live tests in one SolidWorks session filled the 10 000 a process may hold,
    and it crashed (heap corruption after 'resource unavailable'). Restart SolidWorks, then
    run the rest."""
    if "sw" not in request.fixturenames:
        yield
        return
    try:
        from solidworks_mcp.rbr import health
        load = health.sldworks_gui()
    except Exception:  # noqa: BLE001 - no reading: the test runs
        load = None
    if load and load.get("gdi_pct", 0) >= GDI_STOP_PCT:
        pytest.exit(f"SolidWorks (pid {load.get('pid')}) holds {load['gdi']} of {load['gdi_quota']} GDI objects "
                    f"({load['gdi_pct']}%): the run stops before it crashes. Save, restart SolidWorks and run "
                    "the remaining tests.", returncode=3)
    yield


@pytest.fixture(autouse=True)
def _quiet_solidworks(request):
    """Each SolidWorks test runs with SolidWorks' window kept still, as every
    tool call of the server does (SP4.0 crashed while sketching otherwise)."""
    if "sw" not in request.fixturenames:
        yield
        return
    with request.getfixturevalue("sw")._quiet_ui():
        yield


@pytest.fixture
def part(sw):
    """A fresh empty part for one test; closed afterwards.

    Every tool must leave its sketches fully defined, so a stray drag in
    SolidWorks cannot move the geometry. Checking that here holds every part
    test to it, whatever the test itself asserts.
    """
    sw.new_part()
    yield sw
    under_defined = []
    try:
        if sw._model is not None:
            under_defined = sw._under_defined_sketches()
    finally:
        try:
            sw.close_part()
        except SolidWorksError:
            pass
    assert not under_defined, f"a tool left under-defined sketches (shown with (-) in SolidWorks): {under_defined}"


# --- assemblies ---------------------------------------------------------------

BLOCK_A = (40.0, 20.0, 10.0)   # saved as block_a.sldprt
BLOCK_B = (20.0, 20.0, 20.0)   # saved as block_b.sldprt


@pytest.fixture(scope="session")
def blocks(sw, tmp_path_factory):
    """Two saved .sldprt blocks to assemble, built with the part tools.

    Assembly tests need components on disk. Building them here keeps the suite
    self-contained (no machine-specific sample files) and hand-calculable.
    """
    directory = tmp_path_factory.mktemp("blocks")
    paths = {}
    for name, size in (("block_a", BLOCK_A), ("block_b", BLOCK_B)):
        sw.new_part()
        sw.add_box(*size)
        paths[name] = sw.save_part(str(directory / f"{name}.sldprt"))["path"]
        sw.close_part()
    return paths


@pytest.fixture
def assembly(sw, blocks):
    """A fresh empty assembly for one test; it and its components close afterwards."""
    sw.new_assembly()
    yield sw
    _close_documents(sw, blocks.values())


def _close_documents(sw, component_paths):
    """Close the current assembly plus the component windows it opened.

    Inserting a component opens its part document, so without this the suite
    would accumulate open documents across tests. By full path: a person's own
    document may carry the same title.
    """
    try:
        sw.close_part()
    except SolidWorksError:
        pass
    for path in component_paths:
        sw._sw.CloseDoc(path)
