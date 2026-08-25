"""The same hermetic test settings, but against Postgres.

Run with `DJANGO_SETTINGS_MODULE=config.settings.test_pg pytest`. The suite is
identical; the point is to prove the code does not depend on sqlite behaviour —
transactions, constraints and the `unique_together` on invoices all behave
differently enough to be worth checking on the database production will use.
"""

import environ

from .test import *  # noqa: F403

env = environ.Env()

DATABASES = {
    'default': env.db(
        'DATABASE_URL',
        default='postgres://opencloud:opencloud@127.0.0.1:5432/opencloud',
    )
}
