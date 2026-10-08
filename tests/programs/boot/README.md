# Reference boots

Kernel entries that place a fixed task graph without init: the boots the product
used before it had init, kept for the source tests of the kernel mechanisms and for
the CPU acceptance campaign, whose probes read the layout of these graphs. They are
built by `tools/build_fixture.sh` (`LAIX_FIXTURE=<name>`); the product is built by
`build.sh` (one kernel, `LAIX_SESSION=<name>`). See `docs/INIT.md`.
