# Copyright 2026 Ubuntu
# See LICENSE file for licensing details.

"""Functions for interacting with the workload.

The intention is that this module could be used outside the context of a charm.
"""

import json
import logging
import urllib.request

logger = logging.getLogger(__name__)


def get_version(port: int = 8000) -> str:
    """Get the version of fastapi_demo that is running.

    Args:
        port: The port where fastapi_demo web server is listening. Defaults to 8000
    """
    response = urllib.request.urlopen(f"http://localhost:{port}/version")
    data = json.loads(response.read())
    return data["version"]
