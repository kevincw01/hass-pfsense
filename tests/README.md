# Firmware polling regression tests

Run from the repository root:

```sh
python3 -m unittest discover -s tests -v
```

The suite imports the actual XML-RPC client and executes the actual polling and
entity method source with lightweight HA dependencies substituted. It does not
start Home Assistant or contact a real router. A loopback XML-RPC server verifies
that an ordinary short request times out while the separate firmware request
can complete, and that the firmware timeout remains bounded.

Other cases cover HTTP/HTTPS and TLS verification preservation, no process-wide
timeout change for firmware checks, overlapping-request prevention, nonblocking
cached-state access, success/failure timestamps, cached data on failure, malformed
responses, and a single recovery log after failures.

The timeouts in the loopback test are scaled down to keep the suite fast; the
production firmware timeout remains 60 seconds. This is a connection/inactivity
timeout, not a strict end-to-end request deadline.
