import math
from .aggregate import add_to_cell, contribution, empty_cell
from .codec import digest, dumps, field, loads, scalar_token
from .mvcc import visible_rows


class Diagnostics:
    def __init__(self, store):
        self.store = store

    def inspect(self, deep=False):
        with self.store.read() as connection:
            head, floor = int(self.store.get("head")), int(self.store.get("floor"))
            integrity = [row[0] for row in connection.execute("PRAGMA integrity_check" if deep else "PRAGMA quick_check")]
            references = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
            counts = {table: connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                      for table in ("collections", "versions", "commits", "index_defs", "index_values", "pins", "consumers",
                                    "aggregate_view_defs", "aggregate_view_cells", "aggregate_view_members")}
            revisions = [row[0] for row in connection.execute("SELECT rev FROM commits WHERE rev>? ORDER BY rev", (floor,))]
            issues = []
            if revisions != list(range(floor + 1, head + 1)):
                issues.append("retained commit range is not contiguous")
            if references:
                issues.append("broken foreign keys")
            if integrity != ["ok"]:
                issues.append("SQLite physical integrity failure")
            if not 0 <= floor <= head:
                issues.append("invalid revision watermarks")
            for pin in connection.execute("SELECT name,rev FROM pins"):
                if not floor <= pin["rev"] <= head:
                    issues.append("read pin outside retained history: " + pin["name"])
            if deep:
                expected = set()
                for definition in connection.execute("SELECT * FROM index_defs"):
                    tokens = set()
                    for row in visible_rows(connection, definition["collection"], head):
                        token = scalar_token(field(loads(row["body"]), definition["field"]))
                        if token is None:
                            continue
                        if definition["unique_flag"] and token in tokens:
                            issues.append("unique index has duplicate source values: " + definition["name"])
                        tokens.add(token)
                        expected.add((definition["collection"], definition["name"], token, row["key"]))
                actual = {tuple(row) for row in connection.execute("SELECT collection,name,token,key FROM index_values")}
                if expected != actual:
                    issues.append("index entries differ from current visible documents")
                for definition in connection.execute("SELECT * FROM aggregate_view_defs"):
                    view = definition["name"]
                    predicate = loads(definition["predicate"]) if definition["predicate"] is not None else None
                    expected_members = {}
                    expected_cells = {}
                    for row in visible_rows(connection, definition["collection"], head):
                        result = contribution(loads(row["body"]), definition["by_path"], definition["measure_path"], predicate)
                        if result is None:
                            continue
                        token, number = result
                        expected_members[row["key"]] = (token, dumps(number) if number is not None else None)
                        add_to_cell(expected_cells.setdefault(token, empty_cell()), number)
                    actual_members = {row["key"]: (row["token"], row["measure_json"]) for row in connection.execute(
                        "SELECT key,token,measure_json FROM aggregate_view_members WHERE view=?", (view,))}
                    if actual_members != expected_members:
                        issues.append("aggregate view members differ from current documents: " + view)
                    actual_cells = {row["token"]: row for row in
                                    connection.execute("SELECT * FROM aggregate_view_cells WHERE view=?", (view,))}
                    if set(actual_cells) != set(expected_cells):
                        issues.append("aggregate view groups differ from current documents: " + view)
                    else:
                        counts_differ = measures_differ = False
                        for token, cell in expected_cells.items():
                            stored = actual_cells[token]
                            stored_sum = loads(stored["sum_json"])
                            if isinstance(stored_sum, int) and isinstance(cell["sum"], int):
                                same_sum = stored_sum == cell["sum"]
                            else:
                                same_sum = math.isclose(stored_sum, cell["sum"], rel_tol=1e-12, abs_tol=1e-12)
                            stored_min = loads(stored["min_json"]) if stored["min_json"] is not None else None
                            stored_max = loads(stored["max_json"]) if stored["max_json"] is not None else None
                            counts_differ = counts_differ or (stored["count"], stored["numeric_count"]) != (cell["count"], cell["numeric_count"])
                            measures_differ = measures_differ or not same_sum or stored_min != cell["min"] or stored_max != cell["max"]
                        if counts_differ:
                            issues.append("aggregate view counts differ from current documents: " + view)
                        if measures_differ:
                            issues.append("aggregate view measures differ from current documents: " + view)
                    if definition["applied_rev"] != head:
                        issues.append("aggregate view is not at current head: " + view)
            return {"ok": not issues, "issues": issues, "head": head, "floor": floor,
                    "database_id": self.store.get("database_id"), "mode": self.store.get("mode"),
                    "counts": counts, "sqlite": integrity, "foreign_keys": references}

    def fingerprint(self, revision=None):
        from .mvcc import checked_revision
        with self.store.read() as connection:
            revision = checked_revision(self.store, revision)
            content = []
            for collection in connection.execute("SELECT name FROM collections ORDER BY name"):
                content.extend({"collection": collection[0], "key": row["key"], "revision": row["rev"],
                                "document": loads(row["body"]), "expires_at": row["expires"]}
                               for row in visible_rows(connection, collection[0], revision))
            return {"revision": revision, "documents": len(content), "sha256": digest(content)}
