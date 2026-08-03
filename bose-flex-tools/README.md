# Bose SoundLink tools

Small, standalone Linux utility for paired Bose SoundLink speakers. It talks
directly to the speaker over Bluetooth and does not use the Bose app, contact
Bose servers, or perform firmware updates.

```bash
python3 bose_flex.py info 68:F2:1F:93:49:0A
python3 bose_flex.py battery 68:F2:1F:93:49:0A
python3 bose_flex.py devices 68:F2:1F:93:49:0A
python3 bose_flex.py rename 68:F2:1F:93:49:0A "Freddie's Bose"
python3 bose_flex.py voice-prompts 68:F2:1F:93:49:0A
python3 bose_flex.py battery-prompt 68:F2:1F:93:49:0A on
python3 bose_flex.py firmware-status 68:F2:1F:93:49:0A
```

The speaker must already be paired with the computer. Names are limited to 31
UTF-8 bytes by the Bose protocol.

`devices` makes read-only paired-device and device-info queries. It reports up
to two sources in the speaker's active multipoint connection and identifies the
computer making the query as `this device`.

`voice-prompts` decodes the same settings response used by the Bose app. The
`battery-prompt` command preserves the current global voice-prompt state and
language while changing only the spoken battery-level option. If firmware says
the option is unsupported, the command refuses to write unless `--force` is
given; the forced mode is experimental and verifies the speaker's response.
`firmware-status` makes only read-only queries and reports whether an update is
idle or partially staged.

The reverse-engineering notes for the removed SoundLink Max battery prompt are
in [`research/battery-prompts.md`](research/battery-prompts.md). The read-only
multipoint source-name packets are documented in
[`research/connected-devices.md`](research/connected-devices.md).
