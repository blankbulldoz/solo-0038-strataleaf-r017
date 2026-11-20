from .aggregate import add_to_cell, contribution, empty_cell, finish_cell
from .codec import dumps, field, loads, name
from .errors import Conflict, DatabaseError, NotFound
from .mvcc import visible_rows
from .predicates import validate_predicate


def _definition(row):
    return {"name": row["name"], "collection": row["collection"], "by": row["by_path"],
            "measure": row["measure_path"],
            "predicate": loads(row["predicate"]) if row["predicate"] is not None else None,
            "revision": row["applied_rev"]}


def _fetch(connection, view):
    return connection.execute("SELECT * FROM aggregate_view_defs WHERE name=?", (view,)).fetchone()


def _build(connection, view, collection, by, measure, predicate, revision):
    """Replace a view's members and cells with a fresh scan of the documents visible at revision."""
    connection.execute("DELETE FROM aggregate_view_members WHERE view=?", (view,))
    connection.execute("DELETE FROM aggregate_view_cells WHERE view=?", (view,))
    cells = {}
    for row in visible_rows(connection, collection, revision):
        result = contribution(loads(row["body"]), by, measure, predicate)
        if result is None:
            continue
        token, number = result
        connection.execute("INSERT INTO aggregate_view_members VALUES (?,?,?,?)",
                           (view, row["key"], token, dumps(number) if number is not None else None))
        cell = cells.setdefault(token, empty_cell())
        add_to_cell(cell, number)
    for token, cell in cells.items():
        connection.execute("INSERT INTO aggregate_view_cells VALUES (?,?,?,?,?,?,?)",
                           (view, token, cell["count"], cell["numeric_count"], dumps(cell["sum"]),
                            dumps(cell["min"]) if cell["min"] is not None else None,
                            dumps(cell["max"]) if cell["max"] is not None else None))


def _adjust_cell(connection, view, token, measure, sign):
    row = connection.execute("SELECT * FROM aggregate_view_cells WHERE view=? AND token=?", (view, token)).fetchone()
    if row is None:
        if sign < 0:
            raise DatabaseError("aggregate view cell is missing for a recorded member")
        cell = empty_cell()
    else:
        cell = {"count": row["count"], "numeric_count": row["numeric_count"], "sum": loads(row["sum_json"]),
                "min": loads(row["min_json"]) if row["min_json"] is not None else None,
                "max": loads(row["max_json"]) if row["max_json"] is not None else None}
    if sign > 0:
        add_to_cell(cell, measure)
    else:
        cell["count"] -= 1
        if measure is not None:
            cell["numeric_count"] -= 1
            cell["sum"] -= measure
            if measure == cell["min"] or measure == cell["max"]:
                measures = [loads(item[0]) for item in connection.execute(
                    "SELECT measure_json FROM aggregate_view_members "
                    "WHERE view=? AND token=? AND measure_json IS NOT NULL ORDER BY key", (view, token))]
                cell["min"] = min(measures) if measures else None
                cell["max"] = max(measures) if measures else None
    if cell["count"] == 0:
        connection.execute("DELETE FROM aggregate_view_cells WHERE view=? AND token=?", (view, token))
        return
    connection.execute("""INSERT INTO aggregate_view_cells VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(view,token) DO UPDATE SET count=excluded.count, numeric_count=excluded.numeric_count,
        sum_json=excluded.sum_json, min_json=excluded.min_json, max_json=excluded.max_json""",
        (view, token, cell["count"], cell["numeric_count"], dumps(cell["sum"]),
         dumps(cell["min"]) if cell["min"] is not None else None,
         dumps(cell["max"]) if cell["max"] is not None else None))


def _apply_document(connection, definition, key, document):
    view = definition["name"]
    old = connection.execute("SELECT token, measure_json FROM aggregate_view_members WHERE view=? AND key=?",
                             (view, key)).fetchone()
    if old is not None:
        connection.execute("DELETE FROM aggregate_view_members WHERE view=? AND key=?", (view, key))
        _adjust_cell(connection, view, old["token"],
                     loads(old["measure_json"]) if old["measure_json"] is not None else None, -1)
    if document is None:
        return
    result = contribution(document, definition["by_path"], definition["measure_path"],
                          loads(definition["predicate"]) if definition["predicate"] is not None else None)
    if result is None:
        return
    token, number = result
    connection.execute("INSERT INTO aggregate_view_members VALUES (?,?,?,?)",
                       (view, key, token, dumps(number) if number is not None else None))
    _adjust_cell(connection, view, token, number, 1)


def maintain_views(connection, operations, revision):
    """Advance every persistent aggregate view by one committed batch, inside the same transaction."""
    definitions = connection.execute("SELECT * FROM aggregate_view_defs").fetchall()
    if not definitions:
        return
    for definition in definitions:
        for operation in operations:
            if operation["collection"] == definition["collection"]:
                _apply_document(connection, definition, operation["key"], operation["document"])
    connection.execute("UPDATE aggregate_view_defs SET applied_rev=?", (revision,))


class AggregateViews:
    """Named persistent groupings maintained transactionally by every commit.

    The on-the-fly counterpart that rescans documents per call is Aggregates.
    """

    def __init__(self, store):
        self.store = store

    def create(self, view, collection, by, measure=None, predicate=None):
        name(view)
        name(collection)
        field({}, by)
        if measure is not None:
            field({}, measure)
        validate_predicate(predicate)
        with self.store.atomic(writable=True) as connection:
            if not connection.execute("SELECT 1 FROM collections WHERE name=?", (collection,)).fetchone():
                raise NotFound("unknown collection: " + collection)
            if _fetch(connection, view) is not None:
                raise Conflict("aggregate view already exists: " + view)
            head = int(self.store.get("head"))
            connection.execute("INSERT INTO aggregate_view_defs VALUES (?,?,?,?,?,?)",
                               (view, collection, by, measure,
                                dumps(predicate) if predicate is not None else None, head))
            _build(connection, view, collection, by, measure, predicate, head)
        return self.describe(view)

    def describe(self, view):
        with self.store.read() as connection:
            row = _fetch(connection, view)
            if row is None:
                raise NotFound("unknown aggregate view: " + view)
            result = _definition(row)
            result["groups"] = connection.execute(
                "SELECT COUNT(*) FROM aggregate_view_cells WHERE view=?", (view,)).fetchone()[0]
            result["documents"] = connection.execute(
                "SELECT COUNT(*) FROM aggregate_view_members WHERE view=?", (view,)).fetchone()[0]
            return result

    def list(self):
        with self.store.read() as connection:
            return [_definition(row) for row in connection.execute("SELECT * FROM aggregate_view_defs ORDER BY name")]

    def read(self, view):
        with self.store.read() as connection:
            row = _fetch(connection, view)
            if row is None:
                raise NotFound("unknown aggregate view: " + view)
            groups = []
            for cell in connection.execute("SELECT * FROM aggregate_view_cells WHERE view=? ORDER BY token", (view,)):
                groups.append(finish_cell({
                    "key": loads(cell["token"]), "count": cell["count"], "numeric_count": cell["numeric_count"],
                    "sum": loads(cell["sum_json"]),
                    "min": loads(cell["min_json"]) if cell["min_json"] is not None else None,
                    "max": loads(cell["max_json"]) if cell["max_json"] is not None else None}))
            return {"view": view, "collection": row["collection"], "revision": row["applied_rev"], "groups": groups}

    def rebuild(self, view):
        name(view)
        with self.store.atomic(writable=True) as connection:
            row = _fetch(connection, view)
            if row is None:
                raise NotFound("unknown aggregate view: " + view)
            head = int(self.store.get("head"))
            _build(connection, view, row["collection"], row["by_path"], row["measure_path"],
                   loads(row["predicate"]) if row["predicate"] is not None else None, head)
            connection.execute("UPDATE aggregate_view_defs SET applied_rev=? WHERE name=?", (head, view))
        return self.describe(view)

    def drop(self, view):
        name(view)
        with self.store.atomic(writable=True) as connection:
            if not connection.execute("DELETE FROM aggregate_view_defs WHERE name=?", (view,)).rowcount:
                raise NotFound("unknown aggregate view: " + view)
