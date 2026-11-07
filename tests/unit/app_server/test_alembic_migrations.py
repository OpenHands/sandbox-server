"""Run the app_server Alembic chain up, down to 001 and up again on SQLite."""

from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

import openhands.app_server.config as app_server_config
from openhands.app_server.app_lifespan import oss_app_lifespan_service
from openhands.app_server.services.db_session_injector import DbSessionInjector

ALEMBIC_DIR = Path(oss_app_lifespan_service.__file__).parent / 'alembic'


@pytest.fixture
def alembic_config(tmp_path, monkeypatch):
    for var in ('DB_HOST', 'GCP_DB_INSTANCE'):
        monkeypatch.delenv(var, raising=False)
    db_session = DbSessionInjector(persistence_dir=tmp_path)
    monkeypatch.setattr(
        app_server_config,
        'get_global_config',
        lambda: SimpleNamespace(db_session=db_session),
    )
    config = Config(str(ALEMBIC_DIR / 'alembic.ini'))
    config.set_main_option('script_location', str(ALEMBIC_DIR))
    return config, db_session


def _revision(db_session):
    with db_session.get_db_engine().connect() as connection:
        return connection.execute(
            sa.text('SELECT version_num FROM alembic_version')
        ).scalar()


def test_upgrade_downgrade_upgrade(alembic_config):
    config, db_session = alembic_config
    head = command.ScriptDirectory.from_config(config).get_current_head()

    command.upgrade(config, 'head')
    assert _revision(db_session) == head

    command.downgrade(config, '001')
    assert _revision(db_session) == '001'

    command.upgrade(config, 'head')
    assert _revision(db_session) == head
