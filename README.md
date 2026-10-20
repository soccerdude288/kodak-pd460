# kodak-pd460

Print to a **Kodak PD460** photo printer dock from Linux over Bluetooth. That
includes AirPrint from iPhones, iPads and Macs, by sharing it as a CUPS
printer.

The PD460 (a 4x6 dye-sublimation printer made by Prinics) has no standard
printer protocol. It speaks a vendor protocol over Bluetooth Classic serial.
This project implements that protocol, documented in [PROTOCOL.md](PROTOCOL.md),
and wraps it in a CUPS filter and backend.

Tested on a PD460 from a Raspberry Pi 3B+ (Debian 13, CUPS 2.4), printing from
the command line and via AirPrint from an iPad.

## What's here

| File | Role |
|---|---|
| `kodak_dock.py` | Protocol, image preparation, and a command-line tool |
| `cups/rastertokodakdock` | CUPS filter: raster pages to 1240x1864 JPEGs |
| `cups/kodakdock` | CUPS backend: `kodakdock://AA-BB-CC-DD-EE-FF` |
| `cups/kodak-pd460.ppd` | 4x6 borderless, RGB, 300 dpi |
| `install.sh` | Copies the above to a CUPS host and optionally creates a shared queue |
| `tools/testcard.py` | Test card for checking orientation, crop and colour |
| `tools/calibrate.py` | Ruler card for measuring how much the printer trims per edge |
| `test_kodak_dock.py` | Tests against a fake printer replaying a real session |

## Requirements

Linux with Bluetooth, Python 3.9+ (uses native `AF_BLUETOOTH` sockets, no
extra packages), Pillow (`python3-pil`), and for sharing, CUPS with
`cups-filters` and Avahi.

## Use

Find the printer's address. It shows up as `Kodak Instant - XXXX`:

```sh
bluetoothctl --timeout 20 scan on
```

Disconnect any phone that's connected through the Kodak app, since the printer
accepts one connection at a time. Then:

```sh
python3 kodak_dock.py AA:BB:CC:DD:EE:FF --probe          # handshake only, uses no paper
python3 kodak_dock.py AA:BB:CC:DD:EE:FF photo.jpg -v     # print, logging every frame
python3 kodak_dock.py - photo.jpg --dry-run out.jpg      # just write the prepared JPEG
```

Landscape images are rotated to portrait. `--fit contain` letterboxes instead
of cropping.

### Share it as a network printer (AirPrint)

```sh
HOST=pi@printserver.local ./install.sh AA:BB:CC:DD:EE:FF
```

This installs the filter, backend and PPD, turns on CUPS printer sharing and
creates two shared queues for the same printer:

- `Kodak-PD460`: borderless
- `Kodak-PD460-Border`: with a white border of about 4 mm

Avahi advertises both, and iOS and macOS list them as AirPrint printers. iOS
has no per-printer options, so choosing the look means choosing the printer.
On macOS the border is also available as a Paper Type. A print takes about 90
seconds once the job reaches the printer.

### Positioning

The printer prints a little past the paper's edges, unevenly. The driver
compensates using margins measured on one PD460 (`SAFE_MARGINS` in
`kodak_dock.py`). If your prints are cropped or off-centre, print
`tools/calibrate.py`'s card and read the first visible number on each ruler.

## Tests

```sh
python3 -m unittest test_kodak_dock
```

## Status and limits

- Single photo per job, 4x6 only.
- Colour is close but not identical to the official app, which applies its own
  colour correction (blacks have a slight green cast).
- The edge compensation was measured on one printer; units may differ by a
  few pixels.
- Other Prinics printers (Kodak Mini Shot, Step, other Docks) use related
  protocols; only the PD460 has been tested with this code.

## Credits

The frame format comes from
[honeymaro/kodak-photo-printer](https://github.com/honeymaro/kodak-photo-printer)
(MIT), and from oldmud0's
[early notes](https://gist.github.com/oldmud0/4d8b50cc6fdba0dc301ac78f4f05082d).
The PD460 session flow in [PROTOCOL.md](PROTOCOL.md) was worked out for
interoperability with the official app's behaviour and confirmed on hardware.

Not affiliated with or endorsed by Kodak or Prinics. "Kodak" is a trademark of
its owner and is used here only to identify the hardware.

## License

MIT, see [LICENSE](LICENSE).
