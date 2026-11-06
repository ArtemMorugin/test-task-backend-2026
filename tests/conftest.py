import os
from uuid import uuid4

import pytest
from flask import Flask
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from models import db

DEFAULT_URL = "postgresql+psycopg://crm_sync:local-test-only@localhost:55439/crm_sync_test"


@pytest.fixture(scope="session")
def app():
    url = os.environ.get("TEST_DATABASE_URL", DEFAULT_URL)
    if not (make_url(url).database or "").endswith("_test"):
        raise ValueError("Tests require a dedicated database with a name ending in _test")
    # Every test session owns a separate schema; existing tables are never touched.
    schema = "sync_test_" + uuid4().hex
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    app = Flask(__name__)
    app.config.update(
        SQLALCHEMY_DATABASE_URI=url,
        SQLALCHEMY_ENGINE_OPTIONS={"connect_args": {"options": "-csearch_path=" + schema}},
    )
    db.init_app(app)
    with app.app_context():
        db.create_all()
    yield app
    with app.app_context():
        db.session.remove()
        db.engine.dispose()
    with engine.begin() as connection:
        connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    engine.dispose()


@pytest.fixture(autouse=True)
def database(app):
    with app.app_context():
        with db.engine.begin() as connection:
            connection.execute(text("TRUNCATE abstract_customer RESTART IDENTITY"))
        yield
        db.session.remove()
