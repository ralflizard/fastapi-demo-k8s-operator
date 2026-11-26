#!/usr/bin/env python3

"""Kubernetes charm for a demo app."""

import dataclasses
import logging
import time
import urllib.error

import ops
from charms.data_platform_libs.v0.data_interfaces import (
    DatabaseCreatedEvent,
    DatabaseEndpointsChangedEvent,
    DatabaseRequires,
)
from charms.grafana_k8s.v0.grafana_dashboard import GrafanaDashboardProvider
from charms.loki_k8s.v1.loki_push_api import LogForwarder
from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointProvider

import fastapi_demo

# Log messages can be retrieved using juju debug-log
logger = logging.getLogger(__name__)


DATABASE_RELATION = "database"
DATABASE_NAME = "names_db"


@dataclasses.dataclass(frozen=True, kw_only=True)
class FastAPIConfig:
    """Configuration for the FastAPI demo charm."""

    server_port: int = 8000
    """Default port on which FastAPI is available."""

    def __post_init__(self):
        if self.server_port == 22:
            raise ValueError("Invalid port number, 22 is reserved for SSH")


@dataclasses.dataclass(frozen=True, kw_only=True)
class GetDbInfoAction:
    """fetches database authentication information."""

    show_password: bool
    """Show username and password in output information."""


class FastAPIDemoCharm(ops.CharmBase):
    """Charm the service."""

    def __init__(self, framework: ops.Framework) -> None:
        super().__init__(framework)
        self.container: ops.Container = self.unit.get_container("demo-server")
        self.pebble_service_name: str = "fastapi"
        framework.observe(self.on["demo-server"].pebble_ready, self._on_demo_server_pebble_ready)
        framework.observe(self.on.config_changed, self._on_config_changed)
        framework.observe(self.on.collect_unit_status, self._on_collect_status)
        self.database: DatabaseRequires = DatabaseRequires(
            self,
            relation_name=DATABASE_RELATION,
            database_name=DATABASE_NAME,
        )
        framework.observe(self.database.on.database_created, self._on_database_endpoint)
        framework.observe(self.database.on.endpoints_changed, self._on_database_endpoint)
        framework.observe(self.on.get_db_info_action, self._on_get_db_info_action)

        try:
            config = self.load_config(FastAPIConfig)
        except ValueError as e:
            logger.warning("Unable to add metrics: invalid configuration: %s", e)
        else:
            self._prometheus_scraping: MetricsEndpointProvider = MetricsEndpointProvider(
                self,
                relation_name="metrics-endpoint",
                jobs=[{"static_configs": [{"targets": [f"*:{config.server_port}"]}]}],
                refresh_event=self.on.config_changed,
            )

        self._logging: LogForwarder = LogForwarder(self, relation_name="logging")
        self._grafana_dashboards: GrafanaDashboardProvider = GrafanaDashboardProvider(
            self, relation_name="grafana-dashboard"
        )

    def _on_get_db_info_action(self, event: ops.ActionEvent) -> None:
        """Return information about the integrated database."""
        params = event.load_params(GetDbInfoAction, errors="fail")
        db_data = self.fetch_database_relation_data()
        if not db_data:
            event.fail("No database connected")
            return
        output = {
            "db-host": db_data.get("db_host", None),
            "db-port": db_data.get("db_port", None),
        }
        if params.show_password:
            output.update(
                {
                    "db-username": db_data.get("db_username", None),
                    "db-password": db_data.get("db_password", None),
                }
            )
        event.set_results(output)

    def _on_database_endpoint(
        self, _: DatabaseCreatedEvent | DatabaseEndpointsChangedEvent
    ) -> None:
        """Event is fired when the database is created or its endpoint is changed."""
        self._replan_workload()

    def _on_config_changed(self, _: ops.ConfigChangedEvent) -> None:
        self._replan_workload()

    def _on_collect_status(self, event: ops.CollectStatusEvent) -> None:
        try:
            _ = self.load_config(FastAPIConfig)
        except ValueError as e:
            event.add_status(ops.BlockedStatus(str(e)))
        if not self.model.get_relation(DATABASE_RELATION):
            event.add_status(ops.BlockedStatus("Waiting for database relation"))
        elif not self.database.fetch_relation_data():
            event.add_status(ops.WaitingStatus("Waiting for database relation"))
        try:
            status = self.container.get_service(self.pebble_service_name)
        except (ops.pebble.APIError, ops.pebble.ConnectionError, ops.ModelError):
            event.add_status(ops.MaintenanceStatus("Waiting for Pebble in workload container"))
        else:
            if not status.is_running():
                event.add_status(ops.MaintenanceStatus("Waiting for hte service to start up"))
        # Validate if this is correct.
        event.add_status(ops.ActiveStatus())

    def _get_pebble_layer(self, port: int, env: dict[str, str]) -> ops.pebble.Layer:
        """Pebble layer for the FastAPI demo services."""
        cmd = f"/bin/uvicorn api_demo_server.app:app --host 0.0.0.0 --port {port}"
        service: ops.pebble.ServiceDict = {
            "override": "merge",
            "command": cmd,
            "environment": env,
        }
        layer: ops.pebble.LayerDict = {
            "summary": "FastAPI demo service",
            "description": "pebble config layer for FastAPI demo server",
            "services": {self.pebble_service_name: service},
        }
        return ops.pebble.Layer(layer)

    def _replan_workload(self) -> None:
        """Define and start a workload using the Pebble API."""
        self.unit.status = ops.MaintenanceStatus("Assembling Pebble layers")
        try:
            config = self.load_config(FastAPIConfig)
        except ValueError as e:
            logger.error("Configuration error: %s", e)
            return
        env = self.get_app_environment()
        try:
            self.container.add_layer(
                "fastapi_demo",
                self._get_pebble_layer(config.server_port, env),
                combine=True,
            )
            logger.info("Added updated layer 'fastapi_demo' to Pebble plan")

            self.container.replan()
            logger.info(f"Replanned with '{self.pebble_service_name}' service")
        except (ops.pebble.APIError, ops.pebble.ConnectionError) as e:
            logger.info("Unable to connect to Pebble: %s", e)
            return
        for attempt in range(3):
            if attempt:
                time.sleep(2**attempt)
            try:
                version = fastapi_demo.get_version(config.server_port)
                break
            except urllib.error.HTTPError as error:
                error.close()
                continue
            except urllib.error.URLError:
                continue
        else:
            logger.error("The workload was not available within the expected time")
            raise RuntimeError("workload is not available")
        self.unit.set_workload_version(version)

    def _on_demo_server_pebble_ready(self, _: ops.PebbleReadyEvent) -> None:
        self._replan_workload()

    def get_app_environment(self) -> dict[str, str]:
        """Return a dict of env vars for the app."""
        db_data = self.fetch_database_relation_data()
        if not db_data:
            return {}
        return {
            "POSTGRESQL_DB_HOSTNAME": db_data["db_host"],
            "POSTGRESQL_DB_PORT": db_data["db_port"],
            "POSTGRESQL_DB_USERNAME": db_data["db_username"],
            "POSTGRESQL_DB_PASSWORD": db_data["db_password"],
        }

    def fetch_database_relation_data(self) -> dict[str, str]:
        """Retrieve relation data from a database."""
        relations = self.database.fetch_relation_data()
        logger.debug("Got following database data: %s", relations)
        for data in relations.values():
            if not data:
                continue
            logger.info("New database endpoint is %s", data["endpoints"])
            host, port = data["endpoints"].split(":")
            db_data = {
                "db_host": host,
                "db_port": port,
                "db_username": data["username"],
                "db_password": data["password"],
            }
            return db_data
        return {}


if __name__ == "__main__":  # pragma: nocover
    ops.main(FastAPIDemoCharm)
