"""Extract vCenter inventory and persist it in PostgreSQL."""

from __future__ import annotations

import atexit
import argparse
import logging
import os
import ssl
from datetime import datetime, timezone
from typing import Any, Iterable

import psycopg
from psycopg.types.json import Jsonb
from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim

LOG = logging.getLogger("vcenter_etl")
VERSION = "0.1.1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS vcenter_inventory (
	id BIGSERIAL PRIMARY KEY,
	vcenter_host TEXT NOT NULL,
	collection_started_at TIMESTAMPTZ NOT NULL,
	collection_finished_at TIMESTAMPTZ,
	status TEXT NOT NULL,
	error TEXT
);
CREATE TABLE IF NOT EXISTS vcenter_object (
	inventory_id BIGINT NOT NULL REFERENCES vcenter_inventory(id) ON DELETE CASCADE,
	object_type TEXT NOT NULL,
	object_id TEXT NOT NULL,
	name TEXT,
	parent_id TEXT,
	datacenter TEXT,
	payload JSONB NOT NULL,
	PRIMARY KEY (inventory_id, object_type, object_id)
);
CREATE INDEX IF NOT EXISTS vcenter_object_type_idx ON vcenter_object (object_type);
CREATE INDEX IF NOT EXISTS vcenter_object_name_idx ON vcenter_object (name);
"""


def required_environment() -> dict[str, str]:
	names = ("VCENTER_HOST", "VCENTER_USER", "VCENTER_PASSWORD", "POSTGRES_DSN")
	values = {name: os.getenv(name, "") for name in names}
	missing = [name for name, value in values.items() if not value]
	if missing:
		raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
	return values


def check_config() -> None:
	values = required_environment()
	print(f"vCenter host: {values['VCENTER_HOST']}")
	print("vCenter credentials: configured")
	print("PostgreSQL DSN: configured")


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="Extract vCenter inventory into PostgreSQL")
	parser.add_argument("--version", action="version", version=f"vcenter-etl {VERSION}")
	parser.add_argument(
		"--check-config",
		action="store_true",
		help="validate required environment variables without connecting",
	)
	return parser.parse_args()


def json_value(value: Any) -> Any:
	if value is None or isinstance(value, (str, int, float, bool)):
		return value
	if isinstance(value, datetime):
		return value.isoformat()
	if isinstance(value, (list, tuple)):
		return [json_value(item) for item in value]
	if isinstance(value, dict):
		return {str(key): json_value(item) for key, item in value.items()}
	if hasattr(value, "_moId"):
		return {"moid": value._moId, "type": type(value).__name__}
	return str(value)


def moid(obj: Any) -> str:
	return str(getattr(obj, "_moId", getattr(obj, "key", id(obj))))


def datacenter_name(obj: Any) -> str | None:
	current = obj
	while current:
		if isinstance(current, vim.Datacenter):
			return getattr(current, "name", None)
		current = getattr(current, "parent", None)
	return None


def record(kind: str, obj: Any, payload: dict[str, Any]) -> dict[str, Any]:
	parent = getattr(obj, "parent", None)
	return {
		"object_type": kind,
		"object_id": moid(obj),
		"name": getattr(obj, "name", None),
		"parent_id": moid(parent) if parent else None,
		"datacenter": datacenter_name(obj),
		"payload": json_value(payload),
	}


def datastore_records(datastore: Any) -> Iterable[dict[str, Any]]:
	summary = getattr(datastore, "summary", None)
	yield record("datastore", datastore, {
		"url": getattr(summary, "url", None),
		"type": getattr(summary, "type", None),
		"capacity": getattr(summary, "capacity", None),
		"free_space": getattr(summary, "freeSpace", None),
		"accessible": getattr(summary, "accessible", None),
		"maintenance_mode": getattr(summary, "maintenanceMode", None),
	})
	vmfs = getattr(getattr(datastore, "info", None), "vmfs", None)
	for extent in getattr(vmfs, "extent", []) or []:
		yield record("datastore_extent", extent, {
			"disk_name": getattr(extent, "diskName", None),
			"partition": getattr(extent, "partition", None),
		})


def vm_record(vm: Any) -> dict[str, Any]:
	config = getattr(vm, "config", None)
	summary = getattr(vm, "summary", None)
	guest = getattr(summary, "guest", None)
	return record("vm", vm, {
		"instance_uuid": getattr(config, "instanceUuid", None),
		"bios_uuid": getattr(config, "uuid", None),
		"power_state": getattr(getattr(vm, "runtime", None), "powerState", None),
		"guest_os": getattr(config, "guestFullName", None),
		"guest_id": getattr(config, "guestId", None),
		"cpu_count": getattr(config, "numCpu", None),
		"memory_mb": getattr(config, "memorySizeMB", None),
		"ip_address": getattr(guest, "ipAddress", None),
		"tools_status": getattr(guest, "toolsRunningStatus", None),
		"datastores": [moid(item) for item in getattr(vm, "datastore", []) or []],
		"networks": [getattr(item, "name", None) for item in getattr(vm, "network", []) or []],
	})


def collect(content: Any) -> list[dict[str, Any]]:
	objects: list[dict[str, Any]] = []
	for datacenter in content.rootFolder.childEntity:
		if not isinstance(datacenter, vim.Datacenter):
			continue
		objects.append(record("datacenter", datacenter, {"moid": moid(datacenter)}))
		for cluster in datacenter.hostFolder.childEntity:
			if not isinstance(cluster, (vim.ClusterComputeResource, vim.ComputeResource)):
				continue
			objects.append(record("cluster", cluster, {
				"overall_status": getattr(getattr(cluster, "summary", None), "overallStatus", None),
				"host_count": len(getattr(cluster, "host", []) or []),
			}))
			for host in getattr(cluster, "host", []) or []:
				runtime = getattr(host, "runtime", None)
				objects.append(record("esxi", host, {
					"product": getattr(getattr(getattr(host, "config", None), "product", None), "fullName", None),
					"connection_state": getattr(runtime, "connectionState", None),
					"maintenance_mode": getattr(runtime, "inMaintenanceMode", None),
					"hardware": getattr(host, "hardware", None),
				}))
				for datastore in getattr(host, "datastore", []) or []:
					objects.extend(datastore_records(datastore))
				for network in getattr(host, "network", []) or []:
					objects.append(record("network", network, {"name": getattr(network, "name", None)}))
				storage = getattr(getattr(host, "config", None), "storageDevice", None)
				for hba in getattr(storage, "hostBusAdapter", []) or []:
					objects.append(record("san_hba", hba, {"device": hba}))
				for lun in getattr(storage, "scsiLun", []) or []:
					objects.append(record("lun", lun, {
						"canonical_name": getattr(lun, "canonicalName", None),
						"display_name": getattr(lun, "displayName", None),
						"capacity": getattr(lun, "capacity", None),
						"uuid": getattr(lun, "uuid", None),
						"vendor": getattr(lun, "vendor", None),
						"model": getattr(lun, "model", None),
					}))
			objects.extend(vm_record(vm) for vm in getattr(cluster, "vm", []) or [])
	return objects


def extract_and_load() -> int:
	values = required_environment()
	LOG.info("Connecting to vCenter %s", values["VCENTER_HOST"])
	context = ssl._create_unverified_context() if os.getenv("VCENTER_INSECURE", "false").lower() in {"1", "true", "yes"} else ssl.create_default_context()
	service_instance = SmartConnect(host=values["VCENTER_HOST"], user=values["VCENTER_USER"], pwd=values["VCENTER_PASSWORD"], sslContext=context)
	atexit.register(Disconnect, service_instance)
	started = datetime.now(timezone.utc)
	objects = collect(service_instance.RetrieveContent())
	LOG.info("Collected %d vCenter objects", len(objects))
	finished = datetime.now(timezone.utc)
	with psycopg.connect(values["POSTGRES_DSN"]) as connection:
		with connection.cursor() as cursor:
			cursor.execute(SCHEMA)
			cursor.execute("INSERT INTO vcenter_inventory (vcenter_host, collection_started_at, collection_finished_at, status) VALUES (%s, %s, %s, %s) RETURNING id", (values["VCENTER_HOST"], started, finished, "success"))
			inventory_id = cursor.fetchone()[0]
			for item in objects:
				cursor.execute("INSERT INTO vcenter_object (inventory_id, object_type, object_id, name, parent_id, datacenter, payload) VALUES (%s, %s, %s, %s, %s, %s, %s)", (inventory_id, item["object_type"], item["object_id"], item["name"], item["parent_id"], item["datacenter"], Jsonb(item["payload"])))
	LOG.info("Loaded %d objects into inventory %s", len(objects), inventory_id)
	return inventory_id


if __name__ == "__main__":
	logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
	if parse_args().check_config:
		check_config()
	else:
		extract_and_load()