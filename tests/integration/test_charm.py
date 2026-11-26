import json
import logging
from os import path
import pathlib
import time
import urllib.error
import urllib.request

import jubilant
import pytest
import pytest_jubilant
import yaml

logger = logging.getLogger(__name__)

METADATA = yaml.safe_load(pathlib.Path("charmcraft.yaml").read_text())
APP_NAME = METADATA["name"]


@pytest.fixture(scope="module")
def cos(juju_factory: pytest_jubilant.JujuFactory):
    yield juju_factory.get_juju(suffix="cos")


@pytest.mark.juju_setup
def test_deploy(charm: pathlib.Path, juju: jubilant.Juju):
    """Deploy the charm under test."""
    resources = {
        "demo-server-image": METADATA["resources"]["demo-server-image"]["upstream-source"]
    }
    juju.deploy(charm, app=APP_NAME, resources=resources)
    juju.wait(jubilant.all_blocked)

@pytest.mark.juju_setup
def test_database_integration(charm: pathlib.Path, juju: jubilant.Juju):
    """Verify that the charm integrates with the database.

    Assert that the charm is active if the integration is established
    """
    juju.deploy("postgresql-k8s", channel="14/stable", trust=True)
    juju.integrate(APP_NAME, "postgresql-k8s")
    juju.wait(jubilant.all_active)

def test_workload_version_is_set(charm: pathlib.Path, juju: jubilant.Juju):
    """Verify that the workload version has been set."""
    expected_version = "3.0.0"  # Hardcoded for simplicity.
    juju.wait(lambda status: status.apps[APP_NAME].version == expected_version)


@pytest.mark.juju_setup
def test_deploy_cos(charm: pathlib.Path, cos: jubilant.Juju):
    """Deploy COS Lite in a separate model."""
    cos.deploy("cos-lite", trust=True)
    cos.wait(jubilant.all_active, timeout=10 * 60)


@pytest.mark.juju_setup
def test_integrate_loki(charm: pathlib.Path, juju: jubilant.Juju, cos: jubilant.Juju):
    """Integrate our app with Loki from COS Lite."""
    cos.offer("loki", endpoint="logging")
    juju.integrate(APP_NAME, f"{cos.model}.loki")
    juju.wait(jubilant.all_active)
    cos.wait(jubilant.all_active)


def test_loki_data(charm: pathlib.Path, cos: jubilant.Juju):
    """Use Loki's HTTP API to verify that Loki has a label for our app.

    COS Lite exposes Loki's API through the Traefik load balancer.
    Traefik comes with an action that tells us the base URL of Loki's API.
    """
    task = cos.run("traefik/0", "show-proxied-endpoints")
    results = json.loads(task.results["proxied-endpoints"])
    loki_url = results["loki/0"]["url"]
    loki_api_url = f"{loki_url}/loki/api/v1/label/juju_application/values"
    juju_applications = _get_loki_logs(loki_api_url)
    assert juju_applications is not None, "No logs available from Loki"
    assert APP_NAME in juju_applications


def _get_loki_logs(loki_api_url: str) -> list[str] | None:
    """Wait for logs to be available from Loki and return them."""
    for attempt in range(3 * 60):
        if attempt:
            time.sleep(1)
        try:
            response = urllib.request.urlopen(loki_api_url)
        except urllib.error.HTTPError as error:
            error.close()
            continue
        except urllib.error.URLError:
            continue
        response_decoded = json.loads(response.read())
        if "data" in response_decoded:
            return response_decoded["data"]
    return None
