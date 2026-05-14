#!/usr/bin/env python3
import argparse
import json
import os
import select
import signal
import termios
import time
from datetime import datetime
from pathlib import Path


BAUD_MAP = {
    4800: termios.B4800,
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    57600: termios.B57600,
    115200: termios.B115200,
}


running = True


def handle_signal(signum, frame):
    global running
    running = False


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def get_boot_id():
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except Exception:
        return "unknown-boot"


def configure_serial(fd: int, baud: int):
    if baud not in BAUD_MAP:
        raise ValueError(f"Unsupported baud rate: {baud}")

    attrs = termios.tcgetattr(fd)
    attrs[0] = termios.IGNBRK
    attrs[1] = 0
    attrs[2] = BAUD_MAP[baud] | termios.CS8 | termios.CLOCAL | termios.CREAD
    attrs[3] = 0
    attrs[4] = BAUD_MAP[baud]
    attrs[5] = BAUD_MAP[baud]
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 10
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIOFLUSH)


def nmea_checksum_ok(line: str):
    if not line.startswith("$") or "*" not in line:
        return None

    try:
        body, checksum_text = line[1:].split("*", 1)
        checksum_text = checksum_text[:2]
        actual = 0
        for ch in body:
            actual ^= ord(ch)
        expected = int(checksum_text, 16)
        return actual == expected
    except Exception:
        return False


def parse_status(line: str):
    if not line.startswith("$"):
        return {}

    body = line.split("*", 1)[0]
    fields = body.split(",")
    msg = fields[0]
    status = {"last_sentence": msg}

    if msg.endswith("GGA") and len(fields) > 8:
        status["gga_utc"] = fields[1]
        status["fix_quality"] = fields[6]
        status["satellites_used"] = fields[7]
        status["hdop"] = fields[8]
    elif msg.endswith("RMC") and len(fields) > 2:
        status["rmc_utc"] = fields[1]
        status["rmc_status"] = fields[2]
    elif msg.endswith("GSA") and len(fields) > 2:
        status["gsa_fix_type"] = fields[2]
    elif msg.endswith("GSV") and len(fields) > 3:
        status["satellites_in_view"] = fields[3]

    return status


def atomic_write_json(path: Path, data: dict):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, path)


class RollingLogger:
    def __init__(self, out_dir: Path, rotate_sec: int, fsync_sec: int):
        self.out_dir = out_dir
        self.rotate_sec = rotate_sec
        self.fsync_sec = fsync_sec
        self.session_dir = None
        self.nmea_file = None
        self.tsv_file = None
        self.part_index = 0
        self.part_started_mono = 0.0
        self.last_fsync_mono = 0.0

    def start(self):
        boot_id = get_boot_id()[:8]
        start_name = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        self.session_dir = self.out_dir / f"session_{start_name}_{boot_id}"
        self.session_dir.mkdir(parents=True, exist_ok=True)

        meta = {
            "session_started_host_time": now_iso(),
            "boot_id": get_boot_id(),
            "note": "Raw NMEA GPS logger. Host time may be wrong before GPS/NTP sync.",
        }
        atomic_write_json(self.session_dir / "session_meta.json", meta)
        self.open_new_part()

    def open_new_part(self):
        self.close()
        self.part_started_mono = time.monotonic()
        name = f"part_{self.part_index:04d}"
        nmea_path = self.session_dir / f"{name}.nmea"
        tsv_path = self.session_dir / f"{name}.tsv"

        self.nmea_file = open(nmea_path, "a", buffering=1, encoding="ascii", errors="replace")
        self.tsv_file = open(tsv_path, "a", buffering=1, encoding="utf-8", errors="replace")

        if tsv_path.stat().st_size == 0:
            self.tsv_file.write("host_time\tmonotonic_sec\tnmea\n")

        self.part_index += 1
        self.last_fsync_mono = time.monotonic()

    def write_line(self, line: str):
        now_mono = time.monotonic()

        if now_mono - self.part_started_mono >= self.rotate_sec:
            self.open_new_part()

        self.nmea_file.write(line + "\n")
        self.tsv_file.write(f"{now_iso()}\t{now_mono:.6f}\t{line}\n")

        if now_mono - self.last_fsync_mono >= self.fsync_sec:
            self.fsync()

    def fsync(self):
        for f in [self.nmea_file, self.tsv_file]:
            if f:
                f.flush()
                os.fsync(f.fileno())
        self.last_fsync_mono = time.monotonic()

    def close(self):
        for f in [self.nmea_file, self.tsv_file]:
            if f:
                try:
                    f.flush()
                    os.fsync(f.fileno())
                    f.close()
                except Exception:
                    pass
        self.nmea_file = None
        self.tsv_file = None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="/dev/serial0")
    parser.add_argument("--baud", type=int, default=9600)
    parser.add_argument("--out", default="/var/log/gps-logger")
    parser.add_argument("--rotate-sec", type=int, default=300)
    parser.add_argument("--fsync-sec", type=int, default=2)
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    fd = os.open(args.port, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
    configure_serial(fd, args.baud)

    logger = RollingLogger(out_dir, args.rotate_sec, args.fsync_sec)
    logger.start()

    status = {
        "started_at": now_iso(),
        "port": args.port,
        "baud": args.baud,
        "total_lines": 0,
        "checksum_ok": 0,
        "checksum_ng": 0,
        "last_update": None,
    }

    latest_status_path = out_dir / "latest_status.json"
    buffer = ""

    try:
        while running:
            readable, _, _ = select.select([fd], [], [], 1.0)
            if not readable:
                continue

            data = os.read(fd, 4096)
            if not data:
                continue

            buffer += data.decode("ascii", errors="replace")

            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip("\r\n \t")
                if not line or not line.startswith("$"):
                    continue

                checksum = nmea_checksum_ok(line)
                status["total_lines"] += 1
                status["last_update"] = now_iso()
                status["last_raw"] = line

                if checksum is True:
                    status["checksum_ok"] += 1
                elif checksum is False:
                    status["checksum_ng"] += 1

                status.update(parse_status(line))
                logger.write_line(line)

                if status["total_lines"] % 5 == 0:
                    atomic_write_json(latest_status_path, status)
    finally:
        try:
            atomic_write_json(latest_status_path, status)
        except Exception:
            pass
        logger.close()
        os.close(fd)


if __name__ == "__main__":
    main()
