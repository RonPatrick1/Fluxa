# Connected-device BMAP packets

The SoundLink Max exposes its paired and currently connected source devices
through read-only BMAP block `0x04`.

## Paired-device list

Request:

```text
04 04 01 00
```

The response payload begins with the connection state/bitmask. The remaining
bytes are a sequence of six-byte Bluetooth addresses. Those raw address bytes
can be passed back unchanged to the device-info query.

## Device info

Request:

```text
04 05 01 06 <six address bytes>
```

The response payload contains the six address bytes, a one-byte device status,
two reserved bytes, and the UTF-8 device name. Observed status values are:

- `0x00`: paired but disconnected
- `0x01`: connected source
- `0x03`: the device making the BMAP query

The mobile and Ubuntu battery helpers query each paired address, retain status
`0x01` and `0x03`, and stop after finding the two possible multipoint sources.
If the optional query fails, they still announce the already-read battery level
using the helper's configured local name.

This layout was cross-checked against the open-source
[`based.c`](https://github.com/bosefirmware/BoseConnect-Linux_based-connect/blob/master/based.c)
implementation. A live read from Elizabeth's Bose on August 3, 2026 returned
`Alien3-Ubuntu` as the querying device and `Ron's S26 Ultra` as the other
connected source.
