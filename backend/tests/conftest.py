import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/ on path

import pytest  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from storyflow.database import Base, make_engine  # noqa: E402


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "storyflow_test.db"


@pytest.fixture
def engine(db_path):
    eng = make_engine(f"sqlite:///{db_path.as_posix()}")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def db(session_factory):
    session = session_factory()
    yield session
    session.close()