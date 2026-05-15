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

DYNAMIC_MODELS = {
    "portable": 0,
    "stationary": 2,
    "pedestrian": 3,
    "automotive": 4,
    "sea": 5,
    "airborne-1g": 6,
    "airborne-2g": 7,
    "airborne-4g": 8,
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


def log_event(message: str):
    print(f"{now_iso()} {message}", flush=True)


def ubx_checksum(payload: bytes) -> bytes:
    ck_a = ck_b = 0
    for b in payload:
        ck_a = (ck_a + b) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return bytes([ck_a, ck_b])


def ubx_msg(cls: int, msg_id: int, payload: bytes) -> bytes:
    body = bytes([cls, msg_id, len(payload) & 0xFF, (len(payload) >> 8) & 0xFF]) + payload
    return bytes([0xB5, 0x62]) + body + ubx_checksum(body)


def write_ubx(fd: int, cls: int, msg_id: int, payload: bytes, delay: float = 0.15):
    os.write(fd, ubx_msg(cls, msg_id, payload))
    time.sleep(delay)


def cfg_prt_uart1_115200() -> bytes:
    return bytes([
        0x01, 0x00, 0x00, 0x00,  # portID=UART1, reserved, txReady
        0xD0, 0x08, 0x00, 0x00,  # mode=8N1
        0x00, 0xC2, 0x01, 0x00,  # baudRate=115200
        0x03, 0x00, 0x03, 0x00,  # inProto=UBX+NMEA, outProto=UBX+NMEA
        0x00, 0x00, 0x00, 0x00,  # flags, reserved
    ])


def cfg_rate_10hz() -> bytes:
    return bytes([
        0x64, 0x00,  # measRate=100ms
        0x01, 0x00,  # navRate=1
        0x01, 0x00,  # timeRef=GPS
    ])


def cfg_nav5_dynamic_model(model: str) -> bytes:
    payload = bytearray(36)
    payload[0:2] = (0x0001).to_bytes(2, "little")  # apply dynModel only
    payload[2] = DYNAMIC_MODELS[model]
    return bytes(payload)


def cfg_sbas_enabled() -> bytes:
    return bytes([
        0x01,                    # mode: SBAS enabled
        0x07,                    # usage: range + differential correction + integrity
        0x03,                    # maxSBAS: use up to 3 prioritized SBAS channels
        0x00,                    # scanmode2: auto scan
        0x00, 0x00, 0x00, 0x00,  # scanmode1: auto scan all valid PRNs
    ])


def cfg_msg_nmea(msg_id: int, uart1_rate: int) -> bytes:
    return bytes([
        0xF0,
        msg_id,
        0x00,        # DDC
        uart1_rate,  # UART1
        0x00,        # UART2
        0x00,        # USB
        0x00,        # SPI
        0x00,
    ])


def cfg_save() -> bytes:
    return bytes([
        0x00, 0x00, 0x00, 0x00,  # clearMask
        0xFF, 0xFF, 0x00, 0x00,  # saveMask
        0x00, 0x00, 0x00, 0x00,  # loadMask
        0x17,                    # deviceMask
    ])


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


def read_nmea_preview(fd: int, seconds: float):
    deadline = time.monotonic() + seconds
    buffer = ""
    result = {
        "count": 0,
        "checksum_ok": 0,
        "checksum_ng": 0,
        "last_raw": None,
    }

    while time.monotonic() < deadline:
        timeout = min(0.2, max(0.0, deadline - time.monotonic()))
        readable, _, _ = select.select([fd], [], [], timeout)
        if not readable:
            continue

        try:
            data = os.read(fd, 4096)
        except BlockingIOError:
            continue

        if not data:
            continue

        buffer += data.decode("ascii", errors="replace")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip("\r\n \t")
            if not line or not line.startswith("$"):
                continue

            checksum = nmea_checksum_ok(line)
            if checksum is False:
                result["checksum_ng"] += 1
                continue

            result["count"] += 1
            result["last_raw"] = line
            if checksum is True:
                result["checksum_ok"] += 1

    return result


def ordered_baud_candidates(preferred_baud: int):
    ordered = []
    for baud in [preferred_baud, 115200, 9600, 38400, 57600, 19200, 4800]:
        if baud in BAUD_MAP and baud not in ordered:
            ordered.append(baud)
    return ordered


def detect_nmea_baud(fd: int, preferred_baud: int, seconds: float):
    attempts = []
    for baud in ordered_baud_candidates(preferred_baud):
        configure_serial(fd, baud)
        info = read_nmea_preview(fd, seconds)
        attempts.append({
            "baud": baud,
            "count": info["count"],
            "checksum_ok": info["checksum_ok"],
            "checksum_ng": info["checksum_ng"],
            "last_raw": info["last_raw"],
        })
        if info["count"] > 0:
            return baud, attempts

    return None, attempts


def configure_gps_for_10hz(fd: int, current_baud: int, dynamic_model: str, enable_sbas: bool, save_config: bool):
    log_event(f"gps startup config: sending 10Hz/115200 settings from {current_baud}bps")
    configure_serial(fd, current_baud)

    if current_baud != 115200:
        write_ubx(fd, 0x06, 0x00, cfg_prt_uart1_115200(), delay=0.4)
        configure_serial(fd, 115200)

    write_ubx(fd, 0x06, 0x08, cfg_rate_10hz())

    if dynamic_model != "none":
        write_ubx(fd, 0x06, 0x24, cfg_nav5_dynamic_model(dynamic_model))

    if enable_sbas:
        write_ubx(fd, 0x06, 0x16, cfg_sbas_enabled())

    for msg_id in [0x00, 0x04]:  # GGA, RMC
        write_ubx(fd, 0x06, 0x01, cfg_msg_nmea(msg_id, 1), delay=0.05)

    for msg_id in [0x02, 0x03, 0x05, 0x01]:  # GSA, GSV, VTG, GLL
        write_ubx(fd, 0x06, 0x01, cfg_msg_nmea(msg_id, 0), delay=0.05)

    if save_config:
        write_ubx(fd, 0x06, 0x09, cfg_save(), delay=0.5)


def prepare_serial_startup(fd: int, args):
    status = {
        "prefer_10hz": args.prefer_10hz,
        "target_baud": args.baud,
        "active_baud": args.baud,
        "configured_10hz": False,
        "fallback": False,
        "detect_attempts": [],
    }

    if not args.prefer_10hz:
        configure_serial(fd, args.baud)
        return args.baud, status

    detected_baud, attempts = detect_nmea_baud(fd, args.baud, args.startup_detect_sec)
    status["detect_attempts"] = attempts
    status["detected_baud"] = detected_baud

    if detected_baud is None:
        log_event("gps startup config: no NMEA detected before configuration; trying target baud anyway")
        detected_baud = args.baud

    try:
        configure_gps_for_10hz(
            fd=fd,
            current_baud=detected_baud,
            dynamic_model=args.startup_dynamic_model,
            enable_sbas=not args.startup_no_sbas,
            save_config=args.startup_save_config,
        )
    except OSError as exc:
        status["configure_error"] = str(exc)
        log_event(f"gps startup config: write failed: {exc}")

    configure_serial(fd, 115200)
    verify = read_nmea_preview(fd, args.startup_verify_sec)
    status["verify_115200"] = verify

    if verify["count"] > 0:
        status["active_baud"] = 115200
        status["configured_10hz"] = True
        log_event("gps startup config: using 115200bps, 10Hz configuration accepted")
        return 115200, status

    fallback_baud, fallback_attempts = detect_nmea_baud(fd, detected_baud, args.startup_detect_sec)
    status["fallback"] = True
    status["fallback_attempts"] = fallback_attempts
    if fallback_baud is not None:
        status["active_baud"] = fallback_baud
        log_event(f"gps startup config: 115200bps not verified; falling back to {fallback_baud}bps")
        return fallback_baud, status

    configure_serial(fd, args.baud)
    status["active_baud"] = args.baud
    log_event(f"gps startup config: no fallback NMEA detected; continuing at {args.baud}bps")
    return args.baud, status


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


def rmc_status_label_ascii(value: str):
    return {
        "A": "valid/fix",
        "V": "invalid/no-fix",
    }.get(value or "", "unknown")


def fix_quality_label_ascii(value: str):
    return {
        "0": "no-fix",
        "1": "gps-fix",
        "2": "dgps-fix",
        "4": "rtk-fixed",
        "5": "rtk-float",
    }.get(value or "", "unknown")


def hdop_label_ascii(value: str):
    hdop = parse_float(value)
    if hdop is None:
        return "unknown"
    if hdop <= 1.0:
        return "excellent"
    if hdop <= 2.0:
        return "good"
    if hdop <= 5.0:
        return "moderate"
    if hdop <= 10.0:
        return "poor"
    return "invalid/very-poor"


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
                f"({fix_quality_label_ascii(status.get('fix_quality', ''))})  "
                f"RMC: {status.get('rmc_status', '-')} "
                f"({rmc_status_label_ascii(status.get('rmc_status', ''))})"
            ),
            (
                f"lat/lon: {status.get('position', '-')}  "
                f"alt(m): {status.get('altitude_m', '-')}"
            ),
            (
                f"sats used: {status.get('satellites_used', '-')}  "
                f"HDOP(confidence): {status.get('hdop', '-')} "
                f"({hdop_label_ascii(status.get('hdop', ''))}, lower is better)"
            ),
            "",
            "recent NMEA:",
        ])

        for recent in self.recent_lines:
            lines.append(recent[:160])

        lines.extend([
            "",
            "Stop: Ctrl+Alt+F2 -> login -> sudo systemctl stop gps-logger",
            "Back: Ctrl+Alt+F1",
        ])

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
    parser.add_argument("--prefer-10hz", action="store_true", help="try to configure GPS to 10Hz/115200bps at startup")
    parser.add_argument("--startup-detect-sec", type=float, default=2.0, help="seconds to probe each baud during startup detection")
    parser.add_argument("--startup-verify-sec", type=float, default=3.0, help="seconds to verify 115200bps after startup configuration")
    parser.add_argument(
        "--startup-dynamic-model",
        choices=["none", *DYNAMIC_MODELS.keys()],
        default="automotive",
        help="dynamic model used when --prefer-10hz configures the GPS",
    )
    parser.add_argument("--startup-no-sbas", action="store_true", help="do not enable SBAS/MSAS during startup GPS configuration")
    parser.add_argument("--startup-save-config", action="store_true", help="save startup GPS configuration to module flash")
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

    fd = os.open(args.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    active_baud, startup_status = prepare_serial_startup(fd, args)

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
        "baud": active_baud,
        "requested_baud": args.baud,
        "startup": startup_status,
        "total_lines": 0,
        "checksum_ok": 0,
        "checksum_ng": 0,
        "last_update": None,
    }
    latest_status_path = out_dir / "latest_status.json"
    atomic_write_json(latest_status_path, status)
    display.render(status, logger.session_dir, "waiting for NMEA data...")

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
