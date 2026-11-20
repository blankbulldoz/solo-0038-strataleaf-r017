from .codec import MISSING, dumps, field, loads
from .errors import InvalidDocument, NotFound
from .mvcc import checked_revision, visible_rows
from .predicates import matches, validate_predicate


def contribution(document, by, measure, predicate):
    """Map a document to (group token, numeric measure or None), or None if it contributes nothing."""
    if not matches(document, predicate):
        return None
    value = field(document, by)
    if value is MISSING:
        return None
    number = field(document, measure) if measure is not None else MISSING
    if not isinstance(number, (int, float)) or isinstance(number, bool):
        number = None
    return dumps(value), number


def empty_cell():
    return {"count": 0, "numeric_count": 0, "sum": 0, "min": None, "max": None}


def add_to_cell(cell, measure):
    cell["count"] += 1
    if measure is None:
        return
    cell["numeric_count"] += 1
    cell["sum"] += measure
    cell["min"] = measure if cell["min"] is None else min(cell["min"], measure)
    cell["max"] = measure if cell["max"] is None else max(cell["max"], measure)


def finish_cell(cell):
    cell["average"] = cell["sum"] / cell["numeric_count"] if cell["numeric_count"] else None
    return cell


class Aggregates:
    def __init__(self, store):
        self.store = store

    def group(self, collection, by, measure=None, predicate=None, revision=None):
        field({}, by)
        if measure is not None:
            field({}, measure)
        validate_predicate(predicate)
        with self.store.read() as connection:
            revision = checked_revision(self.store, revision)
            if not connection.execute("SELECT 1 FROM collections WHERE name=?", (collection,)).fetchone():
                raise NotFound("unknown collection")
            groups = {}
            for row in visible_rows(connection, collection, revision):
                result = contribution(loads(row["body"]), by, measure, predicate)
                if result is None:
                    continue
                token, number = result
                item = groups.setdefault(token, {"key": loads(token), **empty_cell()})
                add_to_cell(item, number)
            output = []
            for token in sorted(groups):
                output.append(finish_cell(groups[token]))
            return {"revision": revision, "groups": output}
