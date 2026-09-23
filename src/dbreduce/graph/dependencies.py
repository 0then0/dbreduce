from dbreduce.models.schema import Schema, TableKey


def dependencies(schema: Schema) -> dict[TableKey, set[TableKey]]:
    graph: dict[TableKey, set[TableKey]] = {table.key: set() for table in schema.tables}
    for fk in schema.foreign_keys:
        graph[fk.child].add(fk.parent)
    return graph


def components(schema: Schema) -> list[tuple[TableKey, ...]]:
    """Iterative reachability SCCs; small table graphs favor a simple implementation."""
    graph = dependencies(schema)
    reach = {}
    for node in graph:
        seen: set[TableKey] = set()
        pending = [node]
        while pending:
            current = pending.pop()
            if current not in seen:
                seen.add(current)
                pending.extend(graph[current] - seen)
        reach[node] = seen
    remaining = set(graph)
    result = []
    while remaining:
        node = min(remaining)
        group = tuple(
            sorted(other for other in remaining if other in reach[node] and node in reach[other])
        )
        result.append(group)
        remaining.difference_update(group)
    return result
