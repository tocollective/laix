# Ethernet broker, driver and IP stack (G7)

[G7](GAP_07_APPLICATION_LAYER.md) · [Device contract](DEVICE_CONTRACT.md) · [Shell](SHELL.md)

Date: 2026-10-08. Status: **implemented; source accepted; CPU accepted in the G7 packaging campaign (2026-10-08, gateway pinged twice, two names resolved, link-down run). The `net` profile is in the CI matrix and the checked-in provenance; no remote CI run.**

The first CPU run found a defect in the acceptance client, not in the stack: the
emulator's DNS upstream answered `no-such-host.invalid` with an address, and the
client treated anything but `-ENOENT`/`-ETIMEDOUT` as a failure while printing a
result the probe accepts. The client now accepts an address or "no such name"
for the missing name and fails on a timeout or a damaged answer.

```sh
LAIX_SESSION=net sh laix/build.sh              # builds the image
LAIX_SESSION=net sh laix/run.sh                # the client prints to the UART
python3 laix/tests/probe_net_cpu.py laix/build/net.img laix/build/net.map
```

## What runs

```text
client ---- IP stack ---- Ethernet driver ---- kernel broker ---- card ---- emulated network
   \------ console server
```

Four tasks ([net_bootstrap.m](../src/kernel/net_bootstrap.m)). Only the driver
holds a device right, `DEVICE_NET`, and the card's interrupt token. The IP service
reaches the wire only through the driver's endpoint; the client reaches the network
only through the IP service and the console. A task that the stack's author did not
write cannot send a frame without going through code that checks it.

## The kernel broker

The card is a DMA engine: it reads and writes physical memory named by two rings of
descriptors. The kernel therefore owns the rings and every frame buffer
([net_device.m](../src/drivers/net_device.m)); the driver never sees an address.

| Syscall | Arguments | Result |
| --- | --- | --- |
| `SYS_NET_INFO` (78) | destination (16 bytes) | MAC (6 bytes), link, then two counters; the length |
| `SYS_NET_SEND` (79) | frame, length 14..1514 | the length, or `-EINVAL`, `-EFAULT`, `-EBUSY`, `-EIO` |
| `SYS_NET_RECV` (80) | destination (at least 1514 bytes), capacity | the length, `-EAGAIN` with the interrupt armed, or `-EIO` |

All three need `DEVICE_NET`. The interrupt (line 8, an IRQ row of the device table) is
used through the existing `SYS_IRQ_WAIT`/`SYS_IRQ_COMPLETE`.

- **Memory.** Eleven pages are allocated and pinned at boot: one for the rings
  (16 receive descriptors, 4 send descriptors), eight for receive buffers and two
  for send buffers, two 2,048-byte buffers to a page. They go back to the
  allocator only after the card's CONTROL is cleared, which stops all DMA (the
  card moves data only inside a store to `TX_KICK` or while the host polls, and
  only while enabled).
- **Send.** The frame is copied from the caller's buffer into a send buffer, the
  descriptor is handed to the card, and `TX_KICK` is stored; the card sends inside
  that store, so the slot is free again on return. A fault or an error flag makes
  the broker fail permanently with `-EIO` until its owner is replaced.
- **Receive.** The oldest filled descriptor is copied out and the slot goes back to
  the card whether or not the copy worked; damaged frames (error flag, under 14
  bytes) are counted and skipped. When the ring is empty the broker acknowledges
  the card's causes, looks once more, then arms the interrupt and returns
  `-EAGAIN`, so a frame that lands during arming is not missed.
- **Death and rollback.** When the owner dies or a boot rollback runs, the card is
  turned off, its causes are cleared and the pages are freed. The device is only
  granted at boot: there is no runtime regrant.

## The driver

[netdrv.m](../user/services/netdrv.m) owns the card for one client and moves frames
over IPC, 16 bytes at a time, through one send and one receive staging buffer:
`PUT` fills the send buffer, `SEND` sends it, `POLL` (with a wait of up to 60
seconds on the interrupt) fetches the next frame, `GET` reads it out and `DONE`
drops it. It parses nothing. A dead card ends the service. A frame of 98 bytes
costs eight messages each way; a 1,514-byte frame about 95.

## The IP service

[ip.m](../user/services/ip.m) is a pull-model stack: it reads and answers frames
while a request waits for its reply, never in the background. It speaks Ethernet
II; ARP (requests, replies, a four-entry cache); IPv4 without options or
fragments, header checksums checked; ICMP echo, both ways; UDP with checksums.
It sends no TCP and receives none. The address is the emulated network's fixed
one (10.0.2.15/24, gateway 10.0.2.2, DNS 10.0.2.3), which the gateway accepts
without DHCP.

| Request ([ipclient.m](../user/services/ipclient.m)) | Does |
| --- | --- |
| `ipInfo` | address, gateway, MAC, link |
| `ipPing(addr, seq, seconds)` | one echo request; the reply's TTL, `-ETIMEDOUT`, or `-EHOSTUNREACH` when ARP got no answer |
| `ipResolve(name, seconds)` | an A query to 10.0.2.3; the first A record, `-ENOENT`, `-ETIMEDOUT`, `-EPROTO` for a damaged answer |

Every wait is bounded: one second per empty poll, and at most 64 frames handled
for one request, so a busy wire cannot hold a request forever. A frame that is not
for the stack or fails a check is dropped.

## Limits

- No TCP, no DHCP, no IPv6, no fragments, no IP options, no routing beyond "on the
  subnet or via the gateway". One client, one request at a time. No generic socket
  API: ping and name lookup are the two applications.
- Frames cross IPC in 16-byte pieces and the stack waits in one-second steps; this
  is for control traffic, not throughput.
- The `net` profile has its own acceptance client and no shell. The shell profile
  already uses five of the six boot task slots ([SHELL.md](SHELL.md#what-runs)); a
  shell with networking needs a larger task table, a G1 decision.
- The emulated network's policy decides what the gateway forwards. The probe
  accepts either an address or "no such name" for `localhost`; whether the
  emulator's DNS server answers a loopback name, and from which resolver, is
  unverified.

## Failure scenarios

| Failure | Behavior |
| --- | --- |
| Driver dies | The broker stops the card and frees the buffers. The IP service's next call returns `-EPIPE` and it ends; neither is under the recovery supervisor yet |
| IP service dies | The driver keeps serving; a replacement would find the driver's staging buffers as they were (a held received frame is delivered first) |
| Card fault | The broker answers `-EIO` and the driver ends |
| Link down (`--no-net`) | `ipInfo` reports it; requests answer `-ENETDOWN` without sending a frame |
| Lost or late frames | Ping and resolve time out with `-ETIMEDOUT` |

## Evidence

Source: [test_net_device](../tests/test_net_device.py) (13: the broker against a card
model with real DMA semantics: ring construction, ownership, bounds, wrap, the
arming race, damaged frames, unreadable buffers, owner death),
[test_netdrv](../tests/test_netdrv.py) (9), [test_ip](../tests/test_ip.py) (12: the
stack against a model of the gateway that also checks every frame the stack sends,
including checksums), [test_netclient](../tests/test_netclient.py) (4: the client
library and the acceptance client end to end through the driver),
[test_net_profile](../tests/test_net_profile.py) (4: authority and routing, rollback
at every construction failure), [test_net_probe](../tests/test_net_probe.py) (2).

CPU: [probe_net_cpu.py](../tests/probe_net_cpu.py) boots the image with the emulated
network and with `--no-net` and reads the UART. **Not run.** The author did not
build WRM sources for this change (project rule): the compiled broker, DMA on the
real card model, the interrupt path and the real gateway's answers are
unexercised.
