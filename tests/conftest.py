import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from busalert.demo import build_demo_gtfs  # noqa: E402
from busalert.static import Network, build_static_db  # noqa: E402


@pytest.fixture(scope="session")
def demo_db(tmp_path_factory):
    d = tmp_path_factory.mktemp("gtfs")
    build_demo_gtfs(d / "g.zip")
    build_static_db(d / "g.zip", d / "static.db")
    return d / "static.db"


@pytest.fixture(scope="session")
def net(demo_db):
    return Network(demo_db)
