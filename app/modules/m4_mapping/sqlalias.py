"""从下游 SQL 里抽出字段引用。

显式写法 `别名.列名` 直接成引用；裸列名只抽出来登记，归属交给映射层：
唯一候选才算确认，多候选进待查，这里一律不猜。
真实取数脚本里出现子查询、CTE、函数调用、`select *` 的情况在文档里写明不覆盖。
"""

from __future__ import annotations

import re

from app import config

FROM_RE = re.compile(
    r"\b(?:from|join)\s+(?P<table>[A-Za-z_][\w.]*)"
    r"(?:\s+(?:as\s+)?(?P<alias>[A-Za-z_]\w*))?",
    re.IGNORECASE,
)
REF_RE = re.compile(r"\b(?P<alias>[A-Za-z_]\w*)\.(?P<column>[A-Za-z_]\w*)\b")
STOP_WORDS = {
    "where", "on", "group", "order", "having", "limit", "union", "left", "right",
    "inner", "outer", "cross", "join", "select", "from", "as", "and", "or", "using",
    "full", "lateral", "window", "qualify", "fetch", "offset", "with", "values",
}

# 裸列抽取专用的排除词：关键字、常量、类型名。别名解析仍只用上面的 STOP_WORDS。
BARE_STOP_WORDS = STOP_WORDS | {
    "by", "not", "null", "is", "in", "exists", "like", "between", "case", "when",
    "then", "else", "end", "distinct", "all", "any", "asc", "desc", "nulls",
    "first", "last", "over", "partition", "rows", "range", "unbounded",
    "preceding", "following", "current", "row", "true", "false", "interval",
    "varchar", "char", "text", "string", "int", "integer", "bigint", "smallint",
    "tinyint", "decimal", "numeric", "number", "float", "double", "real", "date",
    "timestamp", "datetime", "bool", "boolean", "variant",
}
BARE_TOKEN_RE = re.compile(r"\b(?P<token>[A-Za-z_][\w]*)\b")
AS_ALIAS_RE = re.compile(r"\bas\s+[A-Za-z_]\w*", re.IGNORECASE)


def strip_noise(sql: str) -> str:
    text = config.SQL_BLOCK_COMMENT_RE.sub(" ", sql or "")
    text = config.SQL_COMMENT_RE.sub(" ", text)
    text = config.SQL_STRING_RE.sub("''", text)
    return " ".join(text.split())


def extract_bare(text: str, aliases: dict[str, str]) -> list[str]:
    """抽裸列名：去掉限定引用、`as` 别名、关键字、函数名、表名后剩下的标识符。

    只负责"抽出来"，不判断它是不是真的是列；归属由映射层按候选表决定。
    """
    scrubbed = REF_RE.sub(" ", text)
    scrubbed = AS_ALIAS_RE.sub(" ", scrubbed)
    scope = {table.split(".")[-1].lower() for table in aliases.values()}
    scope |= {table.lower() for table in aliases.values()}
    found: set[str] = set()
    for match in BARE_TOKEN_RE.finditer(scrubbed):
        token = match.group("token")
        low = token.lower()
        if low in BARE_STOP_WORDS or low in aliases or low in scope:
            continue
        tail = scrubbed[match.end():].lstrip()
        if tail.startswith("("):
            continue  # 函数名不是列
        found.add(token)
    return sorted(found)


def extract_refs(sql: str) -> dict:
    text = strip_noise(sql)
    aliases: dict[str, str] = {}
    for match in FROM_RE.finditer(text):
        table = match.group("table")
        alias = (match.group("alias") or "").lower()
        if not alias or alias in STOP_WORDS:
            alias = table.split(".")[-1].lower()
        aliases[alias] = table

    refs: list[str] = []
    unresolved: list[str] = []
    for match in REF_RE.finditer(text):
        prefix = match.group("alias")
        column = match.group("column")
        if prefix.lower() in STOP_WORDS:
            continue
        table = aliases.get(prefix.lower())
        if table is None:
            unresolved.append(f"{prefix}.{column}")
            continue
        refs.append(f"{table}.{column}")
    return {
        "sql": sql,
        "aliases": aliases,
        "refs": sorted(set(refs)),
        "unresolved": sorted(set(unresolved)),
        "bare_columns": extract_bare(text, aliases),
        "scope_tables": sorted({table for table in aliases.values()}),
    }


def split_ref(ref: str) -> tuple[str, str]:
    table, _, column = ref.rpartition(".")
    if not table:
        return "", column
    return table, column
