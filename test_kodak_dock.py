"""Tests against a fake printer that follows the PD460 session as seen on
hardware and in the official app's state machine."""

import socket
import struct
import threading
import unittest

import kodak_dock as kd

ANNOUNCE = (0x02, 5, 0x62, 0x36, bytes([0, 3, 0xC6]) + bytes(29))
BATTERY = (0x04, 1, 3, 0xC6, b"\x17")


def frame(op, a1=0, a2=0, a3=0, payload=b""):
    return kd.Frame(op, a1, a2, a3, payload).encode()


class FakePrinter(threading.Thread):
    """Speaks the printer side; records what the host sent."""

    def __init__(self, sock, fault=None):
        super().__init__(daemon=True)
        self.sock = sock
        self.conn = kd.Connection(None, sock=sock)
        self.fault = fault
        self.image = b""
        self.error = None

    def expect(self, opcode):
        f = self.conn.recv()
        if f.opcode != opcode:
            raise AssertionError(f"expected opcode {opcode:#x}, got {f!r}")
        return f

    def run(self):
        try:
            s = self.sock
            s.sendall(frame(0x64))                       # heartbeat
            assert self.expect(0x64).arg1 == 1           # echo
            s.sendall(frame(*ANNOUNCE))                  # version announcement
            start = self.expect(0x02)                    # answered with SESSION_START
            assert (start.arg1, start.arg2) == (1, 37)
            if self.fault:
                s.sendall(frame(0x04, 0, 0, self.fault))
                return
            s.sendall(frame(*BATTERY))                   # status; the job starts on it
            job = self.expect(0x05)
            s.sendall(frame(0x01, 5, 1, 0))              # needs ack
            assert self.expect(0x07).arg1 == 0
            length = struct.unpack(">I", job.payload[:4])[0]
            half = length // 2
            for offset, size in ((0, half), (half, length - half)):  # pull by offset + length
                s.sendall(frame(0x08, 0, 0, 0, struct.pack(">II", offset, size)))
                data = self.expect(0x09)
                assert len(data.payload) == size
                self.image += data.payload
            s.sendall(frame(0x64))                       # heartbeat mid-job
            self.expect(0x64)
            s.sendall(frame(0x04, 3, 1, 1, bytes(8)))    # MEDIA_INFO, arg3=1 is not a fault
            s.sendall(frame(0x04, 6, 0, 0))              # FINISHED
            self.expect(0x01)                            # END_JOB
        except Exception as e:  # surfaced in the test thread
            self.error = e


class PrintJobTest(unittest.TestCase):
    def run_job(self, image, fault=None):
        host, printer = socket.socketpair()
        host.settimeout(5)
        printer.settimeout(5)
        fake = FakePrinter(printer, fault)
        fake.start()
        job = kd.PrintJob(kd.Connection(None, sock=host), image)
        try:
            job.run(response_timeout=5)
        finally:
            fake.join(5)
            host.close()
            printer.close()
        if fake.error:
            raise fake.error
        return fake, job

    def test_full_print_flow(self):
        image = bytes(range(256)) * 300
        fake, job = self.run_job(image)
        self.assertEqual(fake.image, image)
        self.assertEqual(job.session_reply.arg3, 0x36)
        self.assertEqual(job.battery, 0x3C6)

    def test_fault_raises(self):
        with self.assertRaises(kd.PrinterFault) as ctx:
            self.run_job(b"x" * 10, fault=4)
        self.assertEqual(ctx.exception.detail, 4)

    def test_probe_stops_after_status(self):
        host, printer = socket.socketpair()
        host.settimeout(5)

        def fake():
            c = kd.Connection(None, sock=printer)
            printer.sendall(frame(*ANNOUNCE))
            c.recv()  # SESSION_START
            printer.sendall(frame(*BATTERY))

        t = threading.Thread(target=fake, daemon=True)
        t.start()
        job = kd.PrintJob(kd.Connection(None, sock=host), None)
        job.run(response_timeout=5)
        t.join(5)
        host.close()
        printer.close()
        self.assertFalse(job.job_started)


class PrepareImageTest(unittest.TestCase):
    def test_geometry_and_rotation(self):
        from PIL import Image
        import io

        for size in ((1800, 1200), (600, 900), (500, 500)):
            jpeg = kd.prepare_image(Image.new("RGB", size, "red"))
            out = Image.open(io.BytesIO(jpeg))
            self.assertEqual(out.size, (1240, 1864))
            self.assertEqual(out.format, "JPEG")


if __name__ == "__main__":
    unittest.main()
