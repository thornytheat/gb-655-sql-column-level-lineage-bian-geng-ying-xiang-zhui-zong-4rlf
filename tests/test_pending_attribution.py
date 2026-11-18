"""裸列归属与待查来源：抽取、归属、影响清单、证据更新后的重算。

评审当天的影响清单分两档：已确认受影响（限定引用 + 唯一候选裸列）
与待查来源（同名列对上多张表，绝不猜）。证据更新后下一批次重算，
待查任务退出或转为已确认；同输入重跑不新增评审记录，旧批次保留当时的判断。
"""

from __future__ import annotations

from app import store
from app.modules.m4_mapping import mapping, sqlalias
from app.modules.m5_impact import service as impact_service
from app.modules.m6_batches import service as batch_service
from tests.fixtures import expected


def _consumer(name: str, bare: list[str], scope: list[str], **extra) -> dict:
    base = {"name": name, "kind": "task", "refs": [], "bare_columns": bare,
            "scope_tables": scope, "upstream": [], "downstream": []}
    base.update(extra)
    return base


# ---------- 抽取 ----------

def test_bare_columns_and_scope_extracted():
    parsed = sqlalias.extract_refs(
        "select pay_amt, order_id from fct_pay p join dim_order o on 1 = 1"
    )
    assert parsed["refs"] == []
    assert parsed["bare_columns"] == ["order_id", "pay_amt"]
    assert parsed["scope_tables"] == ["dim_order", "fct_pay"]


def test_bare_extraction_skips_keywords_functions_and_aliases():
    parsed = sqlalias.extract_refs(
        "select count(*) as cnt, cast(o.order_id as varchar) "
        "from dim_order o where o.status is not null"
    )
    assert parsed["refs"] == ["dim_order.order_id", "dim_order.status"]
    assert parsed["bare_columns"] == []


def test_bare_column_in_where_clause_counts():
    parsed = sqlalias.extract_refs(
        "select o.order_id from dim_order o where status = 'x'"
    )
    assert parsed["bare_columns"] == ["status"]


def test_qualified_refs_still_extracted_with_bare_present():
    parsed = sqlalias.extract_refs(
        "select o.order_id, order_no from dim_order o"
    )
    assert parsed["refs"] == ["dim_order.order_id"]
    assert parsed["bare_columns"] == ["order_no"]


# ---------- 归属 ----------

def test_attribute_bare_three_way_split():
    fields = expected.make_fields([
        {"field_id": "1", "table": "t_a", "name": "col"},
        {"field_id": "2", "table": "t_b", "name": "col"},
        {"field_id": "3", "table": "t_a", "name": "solo"},
    ])
    consumers = [_consumer("c1", ["col", "solo", "ghost"], ["t_a", "t_b"])]
    out = mapping.attribute_bare(consumers, fields)
    assert out["c1"]["attributed"] == ["t_a.solo"]
    assert len(out["c1"]["pending"]) == 1
    pending = out["c1"]["pending"][0]
    assert pending["column"] == "col"
    assert pending["candidates"] == ["t_a.col", "t_b.col"]
    assert pending["candidate_field_ids"] == ["1", "2"]
    assert pending["reason"] == "REF_AMBIGUOUS"
    assert out["c1"]["dropped"] == ["ghost"]


def test_attributed_bare_resolves_through_identity():
    fields_old = expected.make_fields([{"field_id": "1", "table": "t_a", "name": "solo"}])
    fields_new = expected.make_fields([{"field_id": "1", "table": "t_a", "name": "solo"}])
    consumers = [_consumer("c1", ["solo"], ["t_a"])]
    resolution = mapping.resolve_refs(consumers, fields_old, fields_new, {})
    assert resolution["attributed_count"] == 1
    assert resolution["reachable"] == 1
    item = resolution["resolved"][0]
    assert item["ref"] == "t_a.solo"
    assert item["qualified"] is False
    assert item["via"] == "direct"


def test_attributed_bare_can_break():
    fields_old = expected.make_fields([{"field_id": "1", "table": "t_a", "name": "gone"}])
    fields_new = expected.make_fields([{"field_id": "2", "table": "t_a", "name": "other"}])
    consumers = [_consumer("c1", ["gone"], ["t_a"])]
    resolution = mapping.resolve_refs(consumers, fields_old, fields_new, {})
    assert resolution["broken_count"] == 1
    assert resolution["broken"][0]["qualified"] is False
    assert resolution["broken"][0]["via"] == "deleted"


def test_pending_refs_stay_out_of_confirmed_account():
    fields_old = expected.make_fields([
        {"field_id": "1", "table": "t_a", "name": "col"},
        {"field_id": "2", "table": "t_b", "name": "col"},
    ])
    fields_new = expected.make_fields([
        {"field_id": "1", "table": "t_a", "name": "col"},
        {"field_id": "2", "table": "t_b", "name": "col"},
    ])
    consumers = [_consumer("c1", ["col"], ["t_a", "t_b"])]
    resolution = mapping.resolve_refs(consumers, fields_old, fields_new, {})
    assert resolution["pending_count"] == 1
    assert resolution["total"] == 0  # 待查不算确认引用
    assert resolution["by_field"] == {}


# ---------- 影响清单 ----------

def test_pending_rows_only_when_candidate_breaks():
    resolution = {"pending": [
        {"consumer": "up", "kind": "task", "column": "col",
         "candidates": ["t_a.col", "t_b.col"], "candidate_field_ids": ["1", "2"],
         "reason": "REF_AMBIGUOUS"},
    ]}
    breaking = [{"field_id": "1", "table": "t_a", "old_name": "col"}]
    rows = impact_service.pending_rows(resolution, breaking)
    assert len(rows) == 1
    assert rows[0]["name"] == "up"
    assert rows[0]["status"] == "pending"
    assert rows[0]["breaking"] == ["t_a.col"]
    assert rows[0]["candidates"] == ["t_a.col", "t_b.col"]
    other = [{"field_id": "9", "table": "t_z", "old_name": "other"}]
    assert impact_service.pending_rows(resolution, other) == []


def test_pending_does_not_propagate_downstream():
    """待查不传播：下游节点不会因为上游待查而进清单。"""
    resolution = {"pending": [
        {"consumer": "up", "kind": "task", "column": "col",
         "candidates": ["t_a.col", "t_b.col"], "candidate_field_ids": ["1", "2"],
         "reason": "REF_AMBIGUOUS"},
    ]}
    breaking = [{"field_id": "1", "table": "t_a", "old_name": "col"}]
    names = [row["name"] for row in impact_service.pending_rows(resolution, breaking)]
    assert names == ["up"]


def test_seeded_pending_and_confirmed(run):
    graph = run["impact"]["impact"]
    assert graph["pending_total"] == expected.IMPACT_PENDING
    pending = {row["name"]: row for row in graph["pending"]}
    assert set(pending) == {"ads.rpt_order_mix", "ads.rpt_order_audit"}
    for row in pending.values():
        assert row["column"] == "order_id"
        assert row["candidates"] == ["dim_order.order_id", "fct_pay.order_id"]
        assert row["breaking"] == ["fct_pay.order_id"]
        assert row["reason"] == "REF_AMBIGUOUS"
        assert row["status"] == "pending"
    confirmed = {row["name"] for row in graph["rows"]}
    # 裸列 pay_amt 只有 fct_pay 一个候选，归属后直接进已确认
    assert "ads.rpt_order_mix" in confirmed
    assert "ads.rpt_order_audit" not in confirmed


def test_seeded_summary_counts(run):
    summary = run["impact"]["summary"]
    assert summary["qualified"] == expected.REFS_QUALIFIED
    assert summary["attributed"] == expected.REFS_ATTRIBUTED
    assert summary["pending"] == expected.REFS_PENDING
    assert summary["refs_total"] == expected.REFS_TOTAL
    assert summary["balanced"] is True


# ---------- 证据更新后的重算 ----------

def test_pending_exit_and_convert_on_new_evidence(client):
    first = store.latest_run()
    second = batch_service.execute(refs_file="refs_downstream_v2.json")
    assert second["reused"] is False
    assert second["run_id"] != first["run_id"]
    assert store.count_runs() == 2

    graph = second["impact"]["impact"]
    assert graph["pending_total"] == 0
    confirmed = {row["name"]: row for row in graph["rows"]}
    # 待查 → 已确认：order_id 归属到破坏的 fct_pay.order_id
    assert "fct_pay.order_id" in confirmed["ads.rpt_order_mix"]["fields"]
    # 待查 → 退出：order_id 归属到未变的 dim_order.order_id
    assert "ads.rpt_order_audit" not in confirmed

    # 旧批次保留当时的判断
    old = store.run_by_id(first["run_id"])
    assert old["impact"]["impact"]["pending_total"] == expected.IMPACT_PENDING
    assert store.latest_run()["run_id"] == second["run_id"]


def test_rerun_same_batch_adds_no_review_record(client):
    first = store.latest_run()
    again = batch_service.execute()
    assert again["reused"] is True
    assert again["run_id"] == first["run_id"]
    assert store.count_runs() == 1
    assert again["impact"]["impact"]["rows"] == first["impact"]["impact"]["rows"]
    assert again["impact"]["impact"]["pending"] == first["impact"]["impact"]["pending"]


def test_run_impact_api_keeps_history(client):
    first = store.latest_run()
    second = batch_service.execute(refs_file="refs_downstream_v2.json")

    old = client.get(f"/api/runs/{first['run_id']}/impact")
    assert old.status_code == 200
    body = old.json()
    assert body["pending_total"] == expected.IMPACT_PENDING
    assert {item["name"] for item in body["pending"]} == {
        "ads.rpt_order_mix", "ads.rpt_order_audit"}

    new = client.get(f"/api/runs/{second['run_id']}/impact")
    assert new.status_code == 200
    assert new.json()["pending_total"] == 0

    missing = client.get("/api/runs/fm-nope/impact")
    assert missing.status_code == 404
