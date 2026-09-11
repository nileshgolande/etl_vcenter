# etl_vcenter

Extracts vCenter inventory into PostgreSQL. The collector captures datacenters, clusters, ESXi hosts, VMs, datastores, datastore extents, LUNs, SAN HBAs, and host networks as typed rows in `vcenter_object`; the source payload is retained in JSONB for fields that vary by vSphere version.

## Setup

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Set the variables in `.env.example` in the shell (rename it to `.env` only if using a dotenv loader; this script reads process environment variables), then run:

```powershell
python agent.py
```

Validate the configuration without making a vCenter or PostgreSQL connection:

```powershell
python agent.py --check-config
```

Display the collector version:

```powershell
python agent.py --version
```

The PostgreSQL schema is created automatically. Every run creates an inventory snapshot in `vcenter_inventory`, so historical collections remain queryable. Use `VCENTER_INSECURE=true` only for a controlled lab vCenter with an untrusted certificate.

The vCenter account should have read-only inventory, datastore, host configuration, and network privileges.