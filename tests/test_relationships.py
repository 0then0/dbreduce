import json
from unittest.mock import MagicMock

import pytest

from dbreduce.graph.dependencies import components, dependencies
from dbreduce.models.schema import Schema, Table
from dbreduce.postgres.relationships import load_relationships


def test_composite_relationship_in_dependency_graph(tmp_path):
    schema = Schema((Table(("public", "child"), (), 2), Table(("public", "parent"), (), 2)), ())
    path = tmp_path / "relations.json"
    path.write_text(
        json.dumps(
            {
                "relationships": [
                    {
                        "from": {"table": "child", "columns": ["tenant", "id"]},
                        "to": {"table": "parent", "columns": ["tenant", "id"]},
                    }
                ]
            }
        )
    )
    connection = MagicMock()
    result = load_relationships(connection, schema, path)
    assert result.foreign_keys[0].virtual
    assert result.foreign_keys[0].columns == ("tenant", "id")
    assert dependencies(result)[("public", "child")] == {("public", "parent")}
    assert len(components(result)) == 2
    connection.execute.assert_called_once()


@pytest.mark.parametrize(
    "entry",
    [
        {},
        {"from": {}, "to": {}, "where": {}},
        {
            "from": {"table": "missing", "columns": ["id"]},
            "to": {"table": "parent", "columns": ["id"]},
        },
        {
            "from": {"table": "parent", "columns": ["id", "tenant"]},
            "to": {"table": "parent", "columns": ["id"]},
        },
        {"from": {"table": "parent", "columns": []}, "to": {"table": "parent", "columns": []}},
    ],
)
def test_invalid_relationship_rejected(tmp_path, entry):
    path = tmp_path / "relations.json"
    path.write_text(json.dumps({"relationships": [entry]}))
    with pytest.raises(ValueError):
        load_relationships(MagicMock(), Schema((Table(("public", "parent"), (), 0),), ()), path)
