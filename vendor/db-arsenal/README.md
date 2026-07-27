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

- `instantclient-sdk-linux.x64-19.25.0.0.0dbru.zip` - Oracle Instant Client 19.25 SDK
- `instantclient-sqlplus-linux.x64-19.25.0.0.0dbru.zip` - Oracle Instant Client 19.25 SQL*Plus

**Still missing:** `instantclient-basic-linux.x64-19.25.0.0.0dbru.zip` - this is
the core package (`libclntsh.so.19.1` and friends) that both of the above
depend on. Without it the build falls back to downloading it from
`download.oracle.com`, which may or may not succeed depending on network
routing at build time. Drop it in here (same filename) to make the build
fully self-contained.

Download link (same version, if you need to fetch it again):
https://download.oracle.com/otn_software/linux/instantclient/1925000/instantclient-basic-linux.x64-19.25.0.0.0dbru.zip
