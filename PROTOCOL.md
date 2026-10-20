# Kodak PD460 Bluetooth print protocol

How the Kodak PD460 (a Prinics-built 4x6 dye-sublimation dock printer) takes a
print job over Bluetooth Classic. Everything here was confirmed by printing on
real hardware. The session flow was checked against the behaviour of the
official Android app (`com.prinics.kodak.photoprinter` 5.1.3).

The framing was first documented by
[honeymaro/kodak-photo-printer](https://github.com/honeymaro/kodak-photo-printer)
for the Kodak Mini Shot 3 Retro. On the PD460, several details of that
document do not hold; they are listed under
[Differences from earlier notes](#differences-from-earlier-notes).

## Transport

| | |
|---|---|
| Radio | Bluetooth Classic only (no BLE advertisement) |
| Name | `Kodak Instant - XXXX` |
| Print channel | SPP, RFCOMM channel 1 (`00001101-0000-1000-8000-00805f9b34fb`) |
| Other channel | RFCOMM 22, Apple iAP2 (used by the iOS app; not needed here) |
| Pairing | Not required; the RFCOMM connect is accepted without a PIN |

The printer accepts one connection at a time. If a phone is connected through
the Kodak app, the RFCOMM connect fails with "Host is down".

## Framing

Every message in both directions is an 8-byte header plus a payload:

```
offset 0  opcode   u8
offset 1  arg1     u8
offset 2  arg2     u8
offset 3  arg3     u8
offset 4  length   u32 big endian, payload length only
offset 8  payload
```

**Dispatch is on the opcode (byte 0).** `arg1` carries a sub-state within some
opcodes.

## Printer to host

| Opcode | Meaning | Host reply |
|---|---|---|
| `64` | heartbeat | `64 01 00 00` |
| `02` | announcement: `arg3` is the model id (`0x36` on the PD460); payload is 32 bytes, bytes 1–2 a battery-like value | `02 01 25 00` (session start, protocol revision 37) |
| `04` `arg1=1` | status; `arg2:arg3` is a battery-like value | start the job (see below) |
| `04` `arg1=3` | printing; payload byte 7 is the pass number (1, 2, 3) | none |
| `04` `arg1=6` | finished | `01 00 00 00` (end job) |
| `04` `arg1=4` | error during a job, code in `arg2` (`0x14` = out of paper) | wait; send `0b` (resume) once fixed |
| `04` `arg1=0`, `arg3≠0` | fault, code in `arg3` | abort |
| `01` `arg1=5` | needs acknowledgement | `07 <arg3> 00 00` |
| `08` | data request; payload = offset u32, length u32 | `09 <arg1> <arg2> 00` + exactly `length` bytes from `offset` |

## Host to printer

| Opcode | Meaning |
|---|---|
| `64 01` | heartbeat echo |
| `02 01 25` | session start, in reply to each `02` announcement |
| `05 <copies> 00 00` + 12-byte payload | job start; payload bytes 0–3 are the JPEG length (u32 BE), the rest zero |
| `07 <token>` | acknowledgement, echoing `arg3` of the `01 05` frame |
| `09` + data | image slice |
| `01` | end job |
| `03` | status poll; the official app sends it after 30 s of silence |
| `0b` | resume after an error (the app's "resume" button) |

## Image

- Baseline JPEG, **1240 x 1864, portrait** (4 x 6 in).
- Quality 90 with 4:2:0 chroma subsampling, matching the official app's output.
  Higher quality and 4:4:4 are also accepted.
- **The printer overprints.** The 1240 x 1864 raster comes out at about
  4.2 x 6.3 in, slightly off-centre. Measured with `tools/calibrate.py`, these
  are the pixels that fall off each edge of a finished print (tabs torn off):

  | left | top | right | bottom |
  |---|---|---|---|
  | 45 | 48 | 18 | 48 |

  The visible window is 1177 x 1768, which is 2:3. `prepare_image` lays each
  page out on that window and repeats the edge pixels into the trimmed border.
  The top and bottom 48 px land on the tear-off tabs.

## A complete print

Real trace, `<-` printer to host, `->` host to printer:

```
<- 64 00 00 00                      heartbeat
-> 64 01 00 00                      echo
<- 02 05 62 36  len=32  00 03 c9..  announcement, model 0x36
-> 02 01 25 00                      session start
<- 04 01 03 c9  len=1               status
-> 05 01 00 00  len=12  00 02 9c 98 00..   job start, 171160 bytes
<- 01 05 01 00                      needs ack
-> 07 00 00 00                      ack
<- 08 00 00 00  len=8   00000000 00008000  offset 0, 32768 bytes
-> 09 00 00 00  len=32768  ff d8 ff e0 ..
<- 08 00 00 00  len=8   00008000 00008000
-> 09 00 00 00  len=32768
   ... 32 KB at a time ...
<- 08 00 00 00  len=8   00027f68 00001d30  last 7472 bytes
-> 09 00 00 00  len=7472
<- 04 03 01 01  len=8   .. 00          pass 1 (the printer beeps)
<- 04 03 01 01  len=8   .. 02          pass 2
<- 04 03 01 01  len=8   .. 03          pass 3
<- 04 06 00 00                      finished
-> 01 00 00 00                      end job
```

About 90 seconds from connect to finished.

## Errors during a job

With no paper, the printer accepts the job and the data, reports a first pass
(`04 03 .. ff`), then about 20 s later `04 04 14 00`: out of paper. It answers
every status poll with the same frame and **does not recover by itself** once
paper is loaded. The host has to send `0b` (resume). Each resume makes the
printer try to feed again (`04 03 .. ff`); if that fails, `04 04 14` comes
back. Once it succeeds, the normal passes (`.. 01`, `02`, `03`) and `04 06`
follow.

The driver keeps the job open for 15 minutes after an error, reporting it to
CUPS (`media-empty-error`, which iOS shows as "out of paper"), and resumes
with back-off (30 s, 1, 2, 4, 8 min) so loading paper at any point continues
the print.

## Differences from earlier notes

These contradict
[honeymaro's PROTOCOL.md](https://github.com/honeymaro/kodak-photo-printer/blob/main/PROTOCOL.md),
at least on the PD460:

1. **Dispatch is on byte 0, not byte 1.** Treating byte 1 as the state
   misreads `02 05 ..` (an announcement) as "needs acknowledgement".
2. **The `02` announcement is answered with session start, not an ACK.**
   Answering it with `07 <arg3>` makes the printer send a zero-length data
   request (`08`, offset 0, length 0) and then loop.
3. **Data requests carry a length.** `08` payload bytes 4–7 give the number of
   bytes wanted. The PD460 asks for 32 KB at a time. Sending the whole image
   in one frame, or sending data the printer did not ask for, gets the job
   accepted (beep, flashing light) but nothing prints.
4. **`04 01 xx yy` with `xx ≠ 0` is a status report**, which the official app
   does not treat as "ready". The PD460 never sends `04 01 00 ..`, but it
   prints when the job is started on the status frame, as the reference
   driver does.
5. **A job start right after a print can be silently ignored.** For a while
   after printing (the status payload byte reads `0x23`–`0x2c` instead of
   about `0x17`, probably head temperature), the printer neither acks `05`
   nor asks for data. It keeps sending `04 01` status frames, roughly one
   per `03` status poll. Sending `05` again on each status frame until it is
   acknowledged starts the job as soon as the printer is ready.
6. **Do not send `70` (device info) in an open session.** The PD460 answers it
   with a data request, as if a job had started.

## Open questions

- The colour correction the official app applies before encoding (blacks print
  with a slight green cast without it).
- The meaning of the battery-like values and the rest of the 32-byte
  announcement payload.
- Whether the modern `10` / `12` job and data opcodes apply to any firmware.
