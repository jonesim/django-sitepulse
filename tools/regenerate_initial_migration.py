"""Regenerate migrations/0001_initial.py and re-apply the partitioned-table patch.

The autodetector cannot know that ``Hit`` needs a partitioned CREATE TABLE, so
this rebuilds the migration and swaps that one operation. Only useful while the
package is pre-release and 0001 can still be rewritten -- after that, changes are
new migrations like anyone else's.

    python tools/regenerate_initial_migration.py
"""

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "src" / "sitepulse" / "migrations" / "0001_initial.py"

if MIGRATION.exists():
    MIGRATION.unlink()

env_path = f"{ROOT / 'src'}{';' if sys.platform == 'win32' else ':'}{ROOT}"
subprocess.run(
    [sys.executable, "-m", "django", "makemigrations", "sitepulse"],
    cwd=ROOT,
    check=True,
    env={
        **dict(__import__("os").environ),
        "PYTHONPATH": env_path,
        "DJANGO_SETTINGS_MODULE": "tests.settings",
    },
)

source = MIGRATION.read_text()
source = source.replace(
    "from django.db import migrations, models\n",
    "from django.db import migrations, models\n\nfrom sitepulse.operations import CreateHitTable\n",
    1,
)
source = source.replace(
    "        migrations.CreateModel(\n            name='Hit',",
    "        # Ordinary CreateModel everywhere except PostgreSQL, where this produces\n"
    "        # a table partitioned by month on `ts`. See sitepulse/partitions.py.\n"
    "        CreateHitTable(\n            name='Hit',",
    1,
)
if "CreateHitTable(" not in source:
    raise SystemExit("could not patch the Hit operation -- check the generated migration")
MIGRATION.write_text(source)
print(f"patched {MIGRATION}")
