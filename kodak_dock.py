#!/usr/bin/env python3
"""Kodak PD460 (Prinics) photo printer: protocol, image prep and CLI.

The printer speaks Prinics' stream protocol over Bluetooth Classic SPP
(RFCOMM channel 1). The framing comes from honeymaro/kodak-photo-printer
(MIT); the PD460 session flow was worked out from the official Android app
`com.prinics.kodak.photoprinter` 5.1.3 and confirmed on hardware. See
PROTOCOL.md.

Frame layout, both directions:

    0  opcode   u8   dispatch is on this
    1  arg1     u8
    2  arg2     u8
    3  arg3     u8   host sends 0 except where noted
    4  length   u32 big endian, payload only
    8  payload

The transfer is printer driven: once the job is started the printer asks for
(offset, length) slices of the JPEG and the host serves exactly those.

Standard library only, except Pillow for image preparation.
"""

import argparse
import io
import socket
import struct
import sys
import time

# Host -> printer opcodes.
END_JOB = 0x01
SESSION_START = 0x02
STATUS_POLL = 0x03
PRINT_START_LEGACY = 0x05
ACK = 0x07
DATA_LEGACY = 0x09
RESUME = 0x0B  # after a printer error is fixed (the app's "resume" button)
PRINT_START = 0x10
DATA = 0x12
HEARTBEAT = 0x64
DEVICE_INFO = 0x70

# Printer -> host opcodes (dispatch is on these, as in the official app).
P_NOTIFY = 0x01   # arg1 == 5: needs ACK echoing arg3
P_ANNOUNCE = 0x02  # firmware/model announcement; answer with SESSION_START
P_STATE = 0x04    # arg1: 1 ready/battery, 3 media, 6 finished
P_REQUEST = 0x08  # payload: offset u32, length u32
JOB_COMPLETE = 0x13
SESSION_END = 0x14

# States carried in arg1 of P_STATE / P_NOTIFY frames.
READY = 1
MEDIA_INFO = 3
ERROR = 4        # P_STATE: printer error, code in arg2 (e.g. 0x14 out of paper or ribbon)
NEEDS_ACK = 5
FINISHED = 6

SESSION_PROTOCOL_REVISION = 37
DEFAULT_JOB_ID = 12231

# Longest silence tolerated while a sheet is printing.
PRINT_TIMEOUT = 300.0
# How long to keep re-offering a job the printer is ignoring (it does that
# while the head cools after a print) before giving up as "cooling down".
ACCEPT_TIMEOUT = 600.0
# How long a job stays open after a printer error (e.g. out of paper), in case
# the user fixes it and the printer carries on.
ERROR_WAIT = 900.0
# First wait before sending RESUME after an error; doubles per attempt, max x16.
ERROR_POLL = 30.0

# PD460 raster geometry: 4x6 portrait.
PD460_WIDTH = 1240
PD460_HEIGHT = 1864

# The printer overprints: of the 1240x1864 raster, these many pixels fall off
# each edge of the finished photo (tabs torn off). Measured on a PD460 with
# tools/calibrate.py. The visible window left over is 1177x1768, i.e. 2:3.
SAFE_MARGINS = (45, 48, 18, 48)  # left, top, right, bottom
# Extra pixels the page extends past the visible window, so paper registration
# never shows a white sliver at an edge.
BLEED = 8
# Width of the optional white border, in raster pixels (about 4 mm on paper).
BORDER = 45

FAULTS = {
    1: "general fault",
    2: "cover open or cartridge not seated",
    3: "paper jam",
    4: "out of paper",
    5: "ribbon or cartridge fault",
    6: "busy with another job",
    7: "ribbon exhausted or media mismatch",
    8: "media mismatch",
    9: "cooling down",
    10: "unrecoverable error",
    # Error codes sent as `04 04 <code> 00` once a job has started.
    # Seen with no paper, and with new paper but a used-up cartridge: the
    # matched paper/ribbon pack is finished.
    0x14: "out of paper or ribbon",
}

# Faults worth retrying later rather than failing the job outright.
TRANSIENT_FAULTS = {6, 9}


class PrinterFault(Exception):
    def __init__(self, detail):
        self.detail = detail
        super().__init__(FAULTS.get(detail, f"unrecognised fault code {detail}"))


class Frame:
    __slots__ = ("opcode", "arg1", "arg2", "arg3", "payload")

    def __init__(self, opcode, arg1=0, arg2=0, arg3=0, payload=b""):
        self.opcode = opcode
        self.arg1 = arg1
        self.arg2 = arg2
        self.arg3 = arg3
        self.payload = payload

    def encode(self):
        header = struct.pack(
            ">BBBBI",
            self.opcode & 0xFF,
            self.arg1 & 0xFF,
            self.arg2 & 0xFF,
            self.arg3 & 0xFF,
            len(self.payload),
        )
        return header + self.payload

    def __repr__(self):
        head = f"{self.opcode:02x} {self.arg1:02x} {self.arg2:02x} {self.arg3:02x}"
        body = self.payload[:48].hex(" ")
        more = "..." if len(self.payload) > 48 else ""
        return f"[{head}] len={len(self.payload)} {body}{more}"


class Connection:
    """An RFCOMM stream to the printer."""

    def __init__(self, address, channel=1, timeout=30.0, log=None, sock=None):
        self.log = log or (lambda msg: None)
        if sock is None:
            sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
            sock.settimeout(timeout)
            sock.connect((address, channel))
        self.sock = sock

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def settimeout(self, seconds):
        self.sock.settimeout(seconds)

    def send(self, frame):
        self.log(f"-> {frame!r}")
        self.sock.sendall(frame.encode())

    def _recv_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("printer closed the connection")
            buf += chunk
        return bytes(buf)

    def recv(self):
        opcode, a1, a2, a3, length = struct.unpack(">BBBBI", self._recv_exact(8))
        payload = self._recv_exact(length) if length else b""
        frame = Frame(opcode, a1, a2, a3, payload)
        self.log(f"<- {frame!r}")
        return frame


class PrintJob:
    """Drives one image through the printer's state machine.

    With image=None it is a probe: it runs the handshake, stops at the first
    status frame and never starts a job, so no media is used.
    """

    def __init__(self, conn, image, copies=1, modern=False, on_status=None, on_progress=None):
        self.conn = conn
        self.image = image
        self.copies = copies
        self.modern = modern
        self.on_status = on_status or (lambda frame: None)
        self.on_progress = on_progress or (lambda sent, total: None)
        self.session_reply = None
        self.device_info = None
        self.media_type = None
        self.data_sent = False
        self.job_started = False
        # Set once the printer acknowledges the job start or asks for data.
        # Until then a hot printer silently ignores job starts, so each status
        # frame gets a fresh one.
        self.accepted = False
        self.temperature = None
        self.first_start = None
        self.error = None  # (code, monotonic time) of an outstanding `04 04` error
        self.resumes = 0
        self.battery = None
        # Start the job on the first status frame. The app waits for a READY
        # with arg2 == 0, but the PD460 only ever sends battery-style status
        # (04 01 03 xx) and prints fine when the job starts on it.
        self.start_on_status = True

    def run(self, response_timeout=30.0, max_polls=3):
        # Dispatch is on the opcode, as in the official app. The printer drives
        # the session: it announces itself (opcode 2) and the host answers that
        # with SESSION_START; it reports state on opcode 4 and asks for data on
        # opcode 8.
        self.conn.settimeout(response_timeout)
        polls = 0
        while True:
            try:
                frame = self.conn.recv()
            except socket.timeout:
                if self.error is not None:
                    # Waiting out a printer error. The printer does not clear it
                    # by itself once fixed (e.g. paper loaded): the host has to
                    # send RESUME. If it is not fixed yet, the error just comes
                    # back, so keep resuming until it clears or time runs out.
                    # Each resume makes the printer try to feed (noisily), so
                    # back off: 30 s, 1, 2, 4, 8 min.
                    if time.monotonic() - self.error[1] > ERROR_WAIT:
                        raise PrinterFault(self.error[0])
                    self.resumes += 1
                    self.conn.send(Frame(RESUME))
                    continue
                # The app polls for status on a read timeout, up to 3 times.
                polls += 1
                if polls > max_polls:
                    raise TimeoutError("printer stopped responding")
                self.conn.send(Frame(STATUS_POLL))
                continue
            polls = 0
            op = frame.opcode

            if op == HEARTBEAT:
                self.conn.send(Frame(HEARTBEAT, 1))
            elif op == P_ANNOUNCE:
                # Firmware/model announcement: model id in arg3, battery in
                # payload[1:3]. The app replies with SESSION_START every time.
                if self.session_reply is None:
                    self.session_reply = frame
                self.conn.send(Frame(SESSION_START, 1, SESSION_PROTOCOL_REVISION))
            elif op == P_NOTIFY:
                if self.job_started:
                    self.accepted = True
                if frame.arg1 == NEEDS_ACK:
                    self.conn.send(Frame(ACK, frame.arg3))
            elif op == P_STATE:
                self.on_status(frame)
                if self._handle_state(frame, response_timeout):
                    return
            elif op == P_REQUEST:
                if self.job_started:
                    self.accepted = True
                self._send_data(frame)
            elif op in (JOB_COMPLETE, SESSION_END):
                return
            elif op == DEVICE_INFO:
                self.device_info = frame.payload

    def _handle_state(self, frame, response_timeout):
        """Handle an opcode 4 status frame. Returns True when the job is over."""
        state = frame.arg1
        if state == 0 and frame.arg3:
            raise PrinterFault(frame.arg3)
        if state == READY:
            if frame.arg2 != 0:
                # Status report, not READY: battery = arg2 << 8 | arg3. The
                # payload byte rises after a print (0x17 cool, 0x23-0x2c
                # warm), probably head temperature; jobs started right after
                # a print can be silently ignored, hence the re-offers below.
                self.battery = (frame.arg2 << 8) | frame.arg3
                if frame.payload:
                    self.temperature = frame.payload[0]
                if self.image is None:
                    return self.session_reply is not None
                if not self.start_on_status:
                    return False
            if frame.arg2 == 0 and frame.arg3 == 10:
                raise PrinterFault(10)
            if self.image is None:
                return True
            if not self.accepted:
                # First start, or the printer ignored the last one while it
                # cooled down (it sends status but never acks or asks for data).
                now = time.monotonic()
                if self.first_start is None:
                    self.first_start = now
                elif now - self.first_start > ACCEPT_TIMEOUT:
                    raise PrinterFault(9)
                self.job_started = True
                self.conn.send(self._job_start())
        elif state == ERROR:
            # The printer stops and reports why; it may carry on once the
            # problem is fixed, so keep the job open for a while.
            now = time.monotonic()
            if self.error is None:
                self.error = (frame.arg2, now)
            elif now - self.error[1] > ERROR_WAIT:
                raise PrinterFault(frame.arg2)
            self.conn.settimeout(ERROR_POLL * 2 ** min(self.resumes, 4))
        elif state == MEDIA_INFO:
            # A resumed print reports a first pass and may still fail to
            # feed, so the error (and its timer) only clears once a later pass
            # arrives.
            if self.error is not None and len(frame.payload) > 7 and frame.payload[7] not in (0, 0xFF):
                self.error = None
                self.resumes = 0
            if len(frame.payload) > 6:
                self.media_type = frame.payload[6]
            # Printing in progress; the dye-sub passes take a while.
            self.conn.settimeout(max(response_timeout, PRINT_TIMEOUT))
        elif state == FINISHED and self.job_started:
            self.conn.send(Frame(END_JOB))
            return True
        return False

    def _job_start(self):
        length = struct.pack(">I", len(self.image))
        if self.modern:
            payload = length + struct.pack(">I", DEFAULT_JOB_ID)
            return Frame(PRINT_START, DEFAULT_JOB_ID >> 8, DEFAULT_JOB_ID, 0, payload)
        return Frame(PRINT_START_LEGACY, self.copies, 0, 0, length + bytes(8))

    def _send_data(self, frame):
        # Payload is offset (u32) then length (u32); serve exactly that slice.
        # The PD460 pulls 32 KB at a time. A zero-length request (seen when the
        # session was mishandled) gets an empty DATA frame, as in the app.
        offset, length = struct.unpack(">II", frame.payload[:8]) if len(frame.payload) >= 8 else (0, 0)
        chunk = self.image[offset : offset + length] if self.image is not None and self.job_started else b""
        opcode = DATA if self.modern else DATA_LEGACY
        self.conn.send(Frame(opcode, frame.arg1, frame.arg2, 0, chunk))
        if chunk:
            self.on_progress(offset + len(chunk), len(self.image))
        if self.job_started and chunk and offset + len(chunk) >= len(self.image):
            self.data_sent = True
            # The printer may go quiet for the whole dye-sub pass, so wait it out.
            self.conn.settimeout(PRINT_TIMEOUT)


def _extend_edges(page, origin, size):
    """Place `page` at `origin` on a canvas of `size`, repeating its edge pixels outward."""
    from PIL import Image

    x0, y0 = origin
    w, h = size
    pw, ph = page.size
    canvas = Image.new("RGB", size)
    canvas.paste(page, origin)
    if y0:
        canvas.paste(page.crop((0, 0, pw, 1)).resize((pw, y0)), (x0, 0))
    if y0 + ph < h:
        canvas.paste(page.crop((0, ph - 1, pw, ph)).resize((pw, h - y0 - ph)), (x0, y0 + ph))
    if x0:
        canvas.paste(canvas.crop((x0, 0, x0 + 1, h)).resize((x0, h)), (0, 0))
    if x0 + pw < w:
        canvas.paste(canvas.crop((x0 + pw - 1, 0, x0 + pw, h)).resize((w - x0 - pw, h)), (x0 + pw, 0))
    return canvas


def prepare_image(
    source,
    width=PD460_WIDTH,
    height=PD460_HEIGHT,
    fit="cover",
    quality=90,
    margins=SAFE_MARGINS,
    bleed=BLEED,
    border=0,
):
    """Return baseline JPEG bytes sized for the printer.

    Matches the official app, which writes the JPEG with stb_image_write at
    quality 90; stb uses 4:2:0 chroma subsampling at that quality.

    `source` is a path, file object or PIL Image. Landscape images are rotated
    to the printer's portrait orientation. `fit` is "cover" (fill and crop,
    borderless) or "contain" (letterbox on white).

    The image is laid out on the part of the raster that actually lands on the
    paper (`margins` trimmed at left, top, right, bottom), extended by `bleed`
    pixels. The rest of the raster repeats the outermost pixels, so it prints
    as a continuation of the edge rather than as white.

    With `border` > 0 the image instead sits inside that window with a white
    border of `border` pixels on every side.
    """
    from PIL import Image, ImageOps

    img = source if isinstance(source, Image.Image) else Image.open(source)
    img = ImageOps.exif_transpose(img).convert("RGB")
    if (img.width > img.height) != (width > height):
        img = img.rotate(90, expand=True)

    left, top, right, bottom = margins
    if border:
        inner = (width - left - right - 2 * border, height - top - bottom - 2 * border)
        if fit == "contain":
            page = ImageOps.pad(img, inner, Image.LANCZOS, color="white")
        else:
            page = ImageOps.fit(img, inner, Image.LANCZOS)
        canvas = Image.new("RGB", (width, height), "white")
        canvas.paste(page, (left + border, top + border))
        return _encode(canvas, quality)

    x0, y0 = max(left - bleed, 0), max(top - bleed, 0)
    box = (min(width - right + bleed, width) - x0, min(height - bottom + bleed, height) - y0)
    if fit == "contain":
        page = ImageOps.pad(img, box, Image.LANCZOS, color="white")
    else:
        page = ImageOps.fit(img, box, Image.LANCZOS)
    return _encode(_extend_edges(page, (x0, y0), (width, height)), quality)


def _encode(img, quality):
    out = io.BytesIO()
    img.save(out, "JPEG", quality=quality, subsampling=2, optimize=False, progressive=False)
    return out.getvalue()


def _cli():
    parser = argparse.ArgumentParser(description="Print to a Kodak PD460 over Bluetooth.")
    parser.add_argument("address", help="printer Bluetooth address, e.g. AA:BB:CC:DD:EE:FF")
    parser.add_argument("image", nargs="?", help="image to print (omit with --probe)")
    parser.add_argument("--channel", type=int, default=1, help="RFCOMM channel (default 1)")
    parser.add_argument("--probe", action="store_true", help="run the handshake and stop; prints nothing")
    parser.add_argument("--modern", action="store_true", help="use the modern 0x10/0x12 opcode pair")
    parser.add_argument("--copies", type=int, default=1)
    parser.add_argument("--fit", choices=("cover", "contain"), default="cover")
    parser.add_argument("--border", action="store_true", help="print with a white border")
    parser.add_argument("--dry-run", metavar="OUT.jpg", help="write the prepared JPEG and exit")
    parser.add_argument("--raw", action="store_true", help="send the image file's bytes unmodified")
    parser.add_argument("-v", "--verbose", action="store_true", help="hex-dump every frame")
    args = parser.parse_args()

    image = None
    if not args.probe:
        if not args.image:
            parser.error("an image is required unless --probe is given")
        if args.raw:
            with open(args.image, "rb") as f:
                image = f.read()
        else:
            image = prepare_image(args.image, fit=args.fit, border=BORDER if args.border else 0)
        if args.dry_run:
            with open(args.dry_run, "wb") as f:
                f.write(image)
            print(f"wrote {len(image)} bytes to {args.dry_run}")
            return 0

    t0 = time.monotonic()
    log = (lambda msg: print(f"{time.monotonic() - t0:6.1f}s {msg}", file=sys.stderr)) if (args.verbose or args.probe) else None
    conn = Connection(args.address, args.channel, log=log)
    try:
        def status(frame):
            if frame.arg1 == MEDIA_INFO:
                print("printing...", file=sys.stderr)

        job = PrintJob(conn, image, copies=args.copies, modern=args.modern, on_status=status)
        started = time.monotonic()
        job.run()
        if args.probe:
            # Don't send anything more: on the PD460 a DEVICE_INFO (0x70) here
            # is answered with a data request, i.e. treated as a job start.
            print(f"printer ready, battery {job.battery}; no job started", file=sys.stderr)
        else:
            print(f"done in {time.monotonic() - started:.0f}s", file=sys.stderr)
    except PrinterFault as e:
        print(f"printer fault {e.detail}: {e}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
