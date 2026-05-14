#!/usr/bin/env python3
import argparse
import json
import os
import select
import signal
import sys
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


def parse_float(text: str):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def parse_nmea_coord(value: str, hemisphere: str):
    if not value or not hemisphere:
        return None

    dot_index = value.find(".")
    if dot_index < 0 or dot_index < 2:
        return None

    degree_len = dot_index - 2
    try:
        degrees = int(value[:degree_len])
        minutes = float(value[degree_len:])
    except ValueError:
        return None

    decimal = degrees + minutes / 60.0
    if hemisphere in ["S", "W"]:
        decimal *= -1

    return decimal


def rmc_status_label(value: str):
    return {
        "A": "有効/fixあり",
        "V": "無効/fixなし",
    }.get(value or "", "不明")


def fix_quality_label(value: str):
    return {
        "0": "fixなし",
        "1": "GPS fixあり",
        "2": "DGPS fixあり",
        "4": "RTK fixed",
        "5": "RTK float",
    }.get(value or "", "不明")


def hdop_label(value: str):
    hdop = parse_float(value)
    if hdop is None:
        return "unknown"
    if hdop <= 1.0:
        return "非常に良い"
    if hdop <= 2.0:
        return "良い"
    if hdop <= 5.0:
        return "普通"
    if hdop <= 10.0:
        return "悪い"
    return "無効/かなり悪い"


def parse_status(line: str):
    if not line.startswith("$"):
        return {}

    body = line.split("*", 1)[0]
    fields = body.split(",")
    msg = fields[0]
    status = {"last_sentence": msg}

    if msg.endswith("GGA") and len(fields) > 8:
        latitude = parse_nmea_coord(fields[2], fields[3]) if len(fields) > 4 else None
        longitude = parse_nmea_coord(fields[4], fields[5]) if len(fields) > 6 else None

        status["gga_utc"] = fields[1]
        status["fix_quality"] = fields[6]
        status["fix_quality_label"] = fix_quality_label(fields[6])
        status["satellites_used"] = fields[7]
        status["hdop"] = fields[8]
        status["hdop_label"] = hdop_label(fields[8])

        if latitude is not None and longitude is not None:
            status["latitude"] = round(latitude, 8)
            status["longitude"] = round(longitude, 8)
            status["position"] = f"{latitude:.8f},{longitude:.8f}"

        if len(fields) > 10:
            status["altitude_m"] = fields[9]
    elif msg.endswith("RMC") and len(fields) > 2:
        latitude = parse_nmea_coord(fields[3], fields[4]) if len(fields) > 5 else None
        longitude = parse_nmea_coord(fields[5], fields[6]) if len(fields) > 7 else None

        status["rmc_utc"] = fields[1]
        status["rmc_status"] = fields[2]
        status["rmc_status_label"] = rmc_status_label(fields[2])

        if latitude is not None and longitude is not None:
            status["latitude"] = round(latitude, 8)
            status["longitude"] = round(longitude, 8)
            status["position"] = f"{latitude:.8f},{longitude:.8f}"

        if len(fields) > 8:
            status["speed_knots"] = fields[7]
            status["course_deg"] = fields[8]
    elif msg.endswith("GSA") and len(fields) > 2:
        status["gsa_fix_type"] = fields[2]
    elif msg.endswith("GSV") and len(fields) > 3:
        status["satellites_in_view"] = fields[3]

    return status


def atomic_write_json(path: Path, data: dict):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, path)


def local_display_connected():
    for status_path in Path("/sys/class/drm").glob("*/status"):
        try:
            if status_path.read_text().strip() == "connected":
                return True
        except Exception:
            continue

    return False


class DisplayMirror:
    def __init__(self, mode: str, target: str, max_lines: int, refresh_sec: float):
        self.mode = mode
        self.target = target
        self.max_lines = max_lines
        self.refresh_sec = refresh_sec
        self.recent_lines = []
        self.last_render_mono = 0.0
        self.stream = None
        self.enabled = self.should_enable()

    def should_enable(self):
        if self.mode == "never":
            return False
        if self.mode == "always":
            return True
        return local_display_connected()

    def open(self):
        if self.stream:
            return True

        try:
            if self.target == "-":
                self.stream = sys.stdout
            else:
                self.stream = open(self.target, "w", buffering=1, encoding="utf-8", errors="replace")
            return True
        except Exception:
            self.enabled = False
            return False

    def update(self, line: str, status: dict, session_dir: Path):
        if not self.enabled:
            return

        self.recent_lines.append(line)
        if len(self.recent_lines) > self.max_lines:
            self.recent_lines = self.recent_lines[-self.max_lines:]

        now_mono = time.monotonic()
        if now_mono - self.last_render_mono < self.refresh_sec:
            return

        self.render(status, session_dir)
        self.last_render_mono = now_mono

    def render(self, status: dict, session_dir: Path, message: str = ""):
        if not self.open():
            return

        lines = [
            "\033[2J\033[H",
            "gps-logger live view",
            f"updated: {status.get('last_update') or '-'}",
            f"session: {session_dir}",
        ]

        if message:
            lines.extend(["", message])

        lines.extend([
            "",
            f"port: {status.get('port')}  baud: {status.get('baud')}",
            (
                f"lines: {status.get('total_lines', 0)}  "
                f"checksum ok/ng: {status.get('checksum_ok', 0)}/{status.get('checksum_ng', 0)}"
            ),
            (
                f"fix: {status.get('fix_quality', '-')} "
                f"({status.get('fix_quality_label', '-')})  "
                f"RMC: {status.get('rmc_status', '-')} "
                f"({status.get('rmc_status_label', '-')})"
            ),
            (
                f"緯度/経度: {status.get('position', '-')}  "
                f"高度(m): {status.get('altitude_m', '-')}"
            ),
            (
                f"使用衛星数: {status.get('satellites_used', '-')}  "
                f"HDOP(信頼度目安): {status.get('hdop', '-')} "
                f"({status.get('hdop_label', '-')}, 低いほど良い)"
            ),
            "",
            "recent NMEA:",
        ])

        for recent in self.recent_lines:
            lines.append(recent[:160])

        try:
            self.stream.write("\n".join(lines) + "\n")
            self.stream.flush()
        except Exception:
            self.enabled = False

    def close(self):
        if self.stream and self.stream is not sys.stdout:
            try:
                self.stream.close()
            except Exception:
                pass
        self.stream = None


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
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--port", default="/dev/ttyAMA0", help="UART device to read NMEA from")
    parser.add_argument("--baud", type=int, default=115200, help="UART baud rate")
    parser.add_argument("--out", default="/var/log/gps-logger", help="log output directory")
    parser.add_argument("--rotate-sec", type=int, default=300, help="seconds per log part")
    parser.add_argument("--fsync-sec", type=int, default=2, help="seconds between fsync calls")
    parser.add_argument("--display", choices=["auto", "always", "never"], default="auto", help="local display mode")
    parser.add_argument("--display-tty", default="/dev/tty1", help="display output target, or '-' for stdout")
    parser.add_argument("--display-lines", type=int, default=12, help="recent NMEA lines shown on display")
    parser.add_argument("--display-refresh-sec", type=float, default=1.0, help="display refresh interval")
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    fd = os.open(args.port, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
    configure_serial(fd, args.baud)

    logger = RollingLogger(out_dir, args.rotate_sec, args.fsync_sec)
    logger.start()
    display = DisplayMirror(
        mode=args.display,
        target=args.display_tty,
        max_lines=max(1, args.display_lines),
        refresh_sec=max(0.1, args.display_refresh_sec),
    )

    status = {
        "started_at": now_iso(),
        "port": args.port,
        "baud": args.baud,
        "total_lines": 0,
        "checksum_ok": 0,
        "checksum_ng": 0,
        "last_update": None,
    }
    display.render(status, logger.session_dir, "waiting for NMEA data...")

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
                display.update(line, status, logger.session_dir)

                if status["total_lines"] % 5 == 0:
                    atomic_write_json(latest_status_path, status)
    finally:
        try:
            atomic_write_json(latest_status_path, status)
        except Exception:
            pass
        display.close()
        logger.close()
        os.close(fd)


if __name__ == "__main__":
    main()
