"""影响面编排：变更字段 → 反向闭包 → 受影响节点、环、断链、待查来源。"""

from __future__ import annotations

from app.modules.m5_impact import broken, closure, cycle


def changed_fields(diff: dict) -> list[dict]:
    """只有破坏项才算影响面；新增字段不会让既有下游报错。"""
    return [
        row for row in diff["rows"]
        if row["verdict"] == "BREAKING" and row["bucket"] != "added"
    ]


def pending_rows(resolution: dict, fields: list[dict]) -> list[dict]:
    """待查来源：裸列候选里含有本次破坏字段的节点。

    来源没查清之前不算确认影响，也不向下游传播；
    候选集合随证据变化，下一批次可能退出或转为已确认。
    """
    breaking = {field["field_id"]: f"{field['table']}.{field['old_name']}"
                for field in fields}
    rows: list[dict] = []
    for item in resolution.get("pending") or []:
        hit = sorted({breaking[field_id]
                      for field_id in item.get("candidate_field_ids") or []
                      if field_id in breaking})
        if not hit:
            continue
        rows.append({
            "name": item["consumer"],
            "kind": item.get("kind") or "task",
            "status": "pending",
            "column": item["column"],
            "candidates": list(item.get("candidates") or []),
            "breaking": hit,
            "reason": item.get("reason") or "REF_AMBIGUOUS",
        })
    return sorted(rows, key=lambda row: (row["name"], row["column"]))


def build(diff: dict, consumers: list[dict], resolution: dict) -> dict:
    fields = changed_fields(diff)
    by_field = {field_id: sorted(names)
                for field_id, names in (resolution.get("by_field") or {}).items()}
    broken_seeds: dict[str, set[str]] = {}
    for item in resolution.get("broken") or []:
        broken_seeds.setdefault(item["consumer"], set()).add(f"断链 {item['ref']}")
    graph = closure.impact(fields, by_field, consumers, broken_seeds=broken_seeds)
    for row in graph["rows"]:
        row["status"] = "confirmed"
    pending = pending_rows(resolution, fields)
    graph["pending"] = pending
    graph["pending_total"] = len(pending)
    cycles = cycle.find_cycles(consumers)
    sides = broken.one_sided(consumers)
    refs = broken.broken_refs(resolution)
    return {
        "impact": graph,
        "cycles": cycles,
        "one_sided": sides,
        "broken": refs,
        "summary": broken.summary(consumers, resolution, cycles),
    }
