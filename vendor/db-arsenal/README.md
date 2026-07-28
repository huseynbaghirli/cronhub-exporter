# db-arsenal

Drop zone for DB client/driver packages (zips, rpms, tarballs) that the
`Dockerfile` needs at build time but that aren't reliably reachable over the
network from every build host (e.g. `download.oracle.com` has been
intermittently unreachable from some networks).

Anything placed here is picked up automatically by the `Dockerfile` - if a
file it needs already exists in this folder, it's used directly (no network
call); if not, it falls back to downloading it. So dropping a new vendor
package here now means one less thing that can fail during `docker build`
later.

## Current contents

- `instantclient-basic-linux.x64-19.25.0.0.0dbru.zip` - Oracle Instant Client 19.25 core (`libclntsh.so.19.1` and friends)
- `instantclient-sdk-linux.x64-19.25.0.0.0dbru.zip` - Oracle Instant Client 19.25 SDK
- `instantclient-sqlplus-linux.x64-19.25.0.0.0dbru.zip` - Oracle Instant Client 19.25 SQL*Plus

All three packages the Dockerfile needs are present, so `docker build` no
longer needs any network access to `download.oracle.com`.

Download link (same version, if any of these ever need to be refreshed):
https://download.oracle.com/otn_software/linux/instantclient/1925000/
