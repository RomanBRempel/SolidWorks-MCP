"""sync_equation_file's pure parts and its COM choreography on a fake EquationMgr."""

import re

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.rbr import equations


# --- parsing --------------------------------------------------------------------------------

def test_parse_reads_globals_dimensions_comments_and_bom():
    text = '﻿"L" = 85\n\n\' a comment\n"D1@Sketch1"=2*"L"\nnot an equation\n"  W  " = "L" / 2\n'
    entries, ignored = equations.parse_equation_file(text)
    assert [e["name"] for e in entries] == ["L", "D1@Sketch1", "W"]
    assert entries[0] == {"line": 1, "name": "L", "rhs": "85", "raw": '"L" = 85', "dimension": False}
    assert entries[1]["dimension"] is True
    assert ignored == [{"line": 5, "text": "not an equation"}]


def test_parse_keeps_the_expression_verbatim():
    [entry], _ = equations.parse_equation_file('"Z_TE_MID"= "Z_TE_BAY" + ( "X_MID" - "X_BAY" ) / 2')
    assert entry["rhs"] == '"Z_TE_BAY" + ( "X_MID" - "X_BAY" ) / 2'


# --- planning -------------------------------------------------------------------------------

def _doc(*rows):
    """doc_entries rows: (index, equation, name, global, broken)."""
    return [{"index": i, "equation": eq, "name": name, "global": glob, "broken": broken}
            for i, eq, name, glob, broken in rows]


def test_plan_sorts_add_update_unchanged_and_missing():
    file_entries, _ = equations.parse_equation_file('"A" = 1\n"B" = 2\n"C"=3\n"D1@S"=4\n')
    doc = _doc((0, '"B" = 99', "B", True, False),
               (1, '"C" =3', "C", True, False),
               (2, '"Gone" = 5', "Gone", True, False),
               (3, '"D2@S" = 1', "D2@S", False, False))
    plan = equations.plan(file_entries, doc)
    assert [e["name"] for e in plan["add"]] == ["A"]
    assert [e["name"] for e in plan["update"]] == ["B"]
    assert plan["update"][0]["index"] == 0 and plan["update"][0]["was"] == '"B" = 99'
    assert [e["name"] for e in plan["unchanged"]] == ["C"]  # whitespace-blind
    assert plan["skipped_dimensions"] == ["D1@S"]
    assert plan["missing_in_file"] == ["Gone"]  # dimension equations are not "missing"


def test_plan_matches_names_case_blind():
    file_entries, _ = equations.parse_equation_file('"width" = 10')
    doc = _doc((0, '"Width" = 10', "Width", True, False))
    plan = equations.plan(file_entries, doc)
    assert not plan["add"] and not plan["update"] and len(plan["unchanged"]) == 1


# --- refusal diagnosis ----------------------------------------------------------------------

def test_refusal_names_the_broken_duplicate():
    doc = _doc((4, '"X" = "D1@Dead"', "X", True, True))
    reason = equations.refusal_reason({"name": "X", "rhs": "5"}, doc, lambda n: False)
    assert "broken equation (index 4" in reason


def test_refusal_names_unresolved_references():
    reason = equations.refusal_reason(
        {"name": "D5@SK_X", "rhs": '-"Z_A" - "B"/2'}, _doc((0, '"B" = 1', "B", True, False)),
        lambda n: n == "B")
    assert "'Z_A'" in reason and "re-created dimension" in reason


def test_refusal_explains_dimension_equations():
    reason = equations.refusal_reason({"name": "D5@SK_X", "rhs": "7"}, [], lambda n: True)
    assert "delete the old equation entry first" in reason


# --- sync on a fake EquationMgr -------------------------------------------------------------

class FakeEqMgr:
    def __init__(self, rows, file_path=""):
        self.rows = list(rows)  # [text, ...]
        self.FilePath = file_path
        self.AutomaticSolveOrder = False
        self.refuse = set()     # lhs names Add2/SetEquation must refuse

    def GetCount(self):
        return len(self.rows)

    def Equation(self, i):
        return self.rows[i]

    def Add2(self, index, text, solve):
        name = re.match(r'\s*"([^"]+)"', text).group(1)
        if name.lower() in self.refuse or any(
                re.match(r'\s*"([^"]+)"', r).group(1).lower() == name.lower() for r in self.rows):
            return -1
        self.rows.append(text)
        return len(self.rows) - 1

    def SetEquation(self, i, text):
        name = re.match(r'\s*"([^"]+)"', text).group(1)
        if name.lower() not in self.refuse:
            self.rows[i] = text

    def Delete(self, i):
        del self.rows[i]
        return 0

    def Value(self, i):
        return 0.0

    def GlobalVariable(self, i):
        return "@" not in re.match(r'\s*"([^"]+)"', self.rows[i]).group(1)


class FakeModel:
    def __init__(self, parameters=()):
        self.parameters = set(parameters)
        self.rebuilds = 0

    def Parameter(self, name):
        return object() if name in self.parameters else None

    def ForceRebuild3(self, flag):
        self.rebuilds += 1
        return True


class FakeSession:
    def __init__(self, eqmgr, model):
        self.eqmgr = eqmgr
        self.model = model

    def _require_model(self):
        return self.model

    def _equation_mgr(self):
        return self.eqmgr

    def _equation_entries(self):
        out = []
        for i, text in enumerate(self.eqmgr.rows):
            name = re.match(r'\s*"([^"]+)"', text).group(1)
            out.append({"index": i, "equation": text, "name": name,
                        "value": 0.0, "global": "@" not in name, "broken": "DEAD" in text})
        return out

    @staticmethod
    def _same_equation(a, b):
        return re.sub(r"\s+", "", a or "").lower() == re.sub(r"\s+", "", b or "").lower()

    def _failing_features(self):
        return []


def test_sync_adds_updates_and_reports(tmp_path):
    file = tmp_path / "params.txt"
    file.write_text('"A" = 1\n"B" = 2\n"D1@S" = 9\n', encoding="utf-8")
    eqmgr = FakeEqMgr(['"B" = 99', '"Gone" = 5'], file_path=str(file))
    session = FakeSession(eqmgr, FakeModel())
    report = equations.sync(session)
    assert report["ok"] is True
    assert [a["equation"] for a in report["added"]] == ['"A" = 1']
    assert [u["equation"] for u in report["updated"]] == ['"B" = 2']
    assert report["skipped_dimensions"] == ["D1@S"]
    assert report["missing_in_file"] == ["Gone"]
    assert eqmgr.AutomaticSolveOrder is True
    assert session.model.rebuilds == 1


def test_sync_deletes_the_broken_duplicate_and_retries(tmp_path):
    file = tmp_path / "params.txt"
    file.write_text('"X" = 7\n', encoding="utf-8")
    eqmgr = FakeEqMgr(['"X" = "DEAD"'], file_path=str(file))  # broken leftover blocks Add2
    session = FakeSession(eqmgr, FakeModel())
    report = equations.sync(session)
    assert report["ok"] is True
    assert report["repaired"] == [{"name": "X", "deleted": '"X" = "DEAD"'}]
    assert eqmgr.rows == ['"X" = 7']


def test_sync_names_the_reason_of_a_refusal(tmp_path):
    file = tmp_path / "params.txt"
    file.write_text('"Y" = "NOWHERE" * 2\n', encoding="utf-8")
    eqmgr = FakeEqMgr([], file_path=str(file))
    eqmgr.refuse = {"y"}
    session = FakeSession(eqmgr, FakeModel())
    report = equations.sync(session)
    assert report["ok"] is False
    [failure] = report["failed"]
    assert failure["line"] == 1 and "'NOWHERE'" in failure["reason"]


def test_sync_without_a_linked_file_asks_for_a_path():
    session = FakeSession(FakeEqMgr([]), FakeModel())
    with pytest.raises(SolidWorksError, match="no linked equation file"):
        equations.sync(session)
