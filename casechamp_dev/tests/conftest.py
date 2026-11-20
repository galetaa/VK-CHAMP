import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from app.main import create_app
from app.settings import Settings
from fastapi.testclient import TestClient

ORIGIN = "http://localhost:8000"
BASE = "/api/v1"


@pytest.fixture(scope="session")
def postgres_url():
    configured = os.environ.get("TEST_DATABASE_URL")
    if configured:
        yield configured
        return
    candidates = [os.environ.get("CASECHAMP_PG_BIN"), "/usr/lib/postgresql/17/bin"]
    if pg_config := shutil.which("pg_config"):
        candidates.append(subprocess.check_output([pg_config, "--bindir"], text=True).strip())
    for candidate in filter(None, candidates):
        binary = Path(candidate)
        if (binary / "postgres").is_file() and "PostgreSQL) 17." in subprocess.check_output(
            [binary / "postgres", "--version"], text=True
        ):
            break
    else:
        pytest.skip("PostgreSQL 17 binaries or a dedicated TEST_DATABASE_URL are required")
    with tempfile.TemporaryDirectory(prefix="casechamp-pg-") as temporary:
        data = Path(temporary) / "data"

        def pg(command_name, *args, timeout=20, **options):
            return subprocess.run(
                [binary / command_name, *args], capture_output=True, check=True, timeout=timeout, **options
            )

        pg("initdb", "-D", data, "--auth=trust", "--encoding=UTF8", "--no-locale")
        pg(
            "pg_ctl",
            "-D",
            data,
            "-l",
            Path(temporary) / "postgres.log",
            "-w",
            "start",
            "-o",
            f"-c listen_addresses='' -k {temporary}",
        )
        try:
            pg("createdb", "-h", temporary, "casechamp")
            url = f"postgresql+psycopg:///casechamp?host={temporary}"
            env = pytest.MonkeyPatch()
            with env.context() as settings:
                settings.setenv("MIGRATION_DATABASE_URL", url)
                command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")), "head")
            yield url
        finally:
            pg("pg_ctl", "-D", data, "-m", "fast", "-w", "stop")


@pytest.fixture
def backend(tmp_path, postgres_url):
    url = postgres_url
    app = create_app(Settings(app_mode="test", database_url=url, media_root=tmp_path / "media"))
    with TestClient(app, base_url=ORIGIN) as client:
        yield client, app.state.service
    app.state.service.engine.dispose()


def call(client, method, path, *, body=None, epoch=None, key=None, **kwargs):
    headers = dict(kwargs.pop("headers", {}))
    if method != "GET":
        headers.setdefault("Origin", ORIGIN)
        headers.setdefault("Idempotency-Key", str(key or uuid4()))
    if epoch is not None:
        headers["X-Sandbox-Epoch"] = str(epoch)
    if body is not None:
        kwargs["json"] = body
    return client.request(method, BASE + path, headers=headers, **kwargs)


def bootstrap(client, fixture="TABLE_DEMO_V1"):
    response = call(client, "POST", "/demo/sandboxes", body={"fixture_id": fixture})
    assert response.status_code == 201, response.text
    return response.json()["data"]


class ControlledClock:
    def __init__(self):
        from datetime import datetime

        self.value = datetime.fromisoformat("2026-11-20T10:00:00.000Z")

    def now(self, db):
        return self.value

    def move(self, stamp):
        from datetime import datetime

        self.value = datetime.fromisoformat(stamp)

    def advance(self, seconds):
        from datetime import timedelta

        self.value += timedelta(seconds=seconds)


@pytest.fixture
def controlled_backend(tmp_path, postgres_url):
    url = postgres_url
    clock = ControlledClock()

    def build(**options):
        return create_app(
            Settings(app_mode="test", database_url=url, media_root=tmp_path / "media", **options), clock=clock
        )

    apps = []

    def start(**options):
        app = build(**options)
        apps.append(app)
        return TestClient(app, base_url=ORIGIN), app.state.service

    yield start, clock
    for app in apps:
        app.state.service.engine.dispose()
