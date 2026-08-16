"""Migration operations."""

from __future__ import annotations

from django.db import migrations

from .partitions import create_hit_table


class CreateHitTable(migrations.CreateModel):
    """``CreateModel`` that produces a partitioned table on PostgreSQL.

    Everything Django knows about the model -- state, later migrations,
    ``makemigrations`` diffing -- is unchanged; only the DDL differs. On any
    other backend this is exactly ``CreateModel``.
    """

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(app_label, self.name)
        if not self.allow_migrate_model(schema_editor.connection, model):
            return
        if schema_editor.connection.vendor == "postgresql":
            create_hit_table(schema_editor, model)
        else:
            schema_editor.create_model(model)

    def describe(self):
        return f"Create partitioned model {self.name}"
