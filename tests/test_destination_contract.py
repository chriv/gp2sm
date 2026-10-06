"""Run the destination contract against the fake (always) and real SmugMug (opt-in).

Live run: GP2SM_LIVE_SMUGMUG=path/to/smugmug_config.json python -m pytest -q -m live
It creates a private sandbox folder, runs the contract inside it, and deletes the folder afterwards.
"""

import datetime
import os

import pytest

from tests.contracts.destination import DestinationContract
from tests.fakes.smugmug import FakeSmugMug


class TestFakeSmugMugContract(DestinationContract):
    @pytest.fixture
    def dest(self):
        yield FakeSmugMug(), "/api/v2/node/SANDBOX"


@pytest.fixture(scope="module")
def live_sandbox():
    from gp2sm.smugmug.client import SmugMugClient
    client = SmugMugClient.from_config_file(os.environ["GP2SM_LIVE_SMUGMUG"])
    name = "gp2sm-contract-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = client.ensure_folder_path(client.root_folder(), name)
    yield client, folder
    client.delete_folder(folder)


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("GP2SM_LIVE_SMUGMUG"), reason="set GP2SM_LIVE_SMUGMUG to a smugmug_config.json")
class TestLiveSmugMugContract(DestinationContract):
    @pytest.fixture
    def dest(self, live_sandbox):
        yield live_sandbox
