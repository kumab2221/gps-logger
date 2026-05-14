#!/usr/bin/env python3
import argparse
import sys
import time

try:
    import serial
except ImportError:
    print("python3-serial is required. Install it with: sudo apt install python3-serial", file=sys.stderr)
    raise


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


def ubx_checksum(payload: bytes) -> bytes:
    ck_a = ck_b = 0
    for b in payload:
        ck_a = (ck_a + b) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return bytes([ck_a, ck_b])


def ubx_msg(cls: int, msg_id: int, payload: bytes) -> bytes:
    body = bytes([cls, msg_id, len(payload) & 0xFF, (len(payload) >> 8) & 0xFF]) + payload
    return bytes([0xB5, 0x62]) + body + ubx_checksum(body)


def write_ubx(ser, cls: int, msg_id: int, payload: bytes, delay: float = 0.15):
    ser.write(ubx_msg(cls, msg_id, payload))
    ser.flush()
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
        0x00,       # DDC
        uart1_rate, # UART1
        0x00,       # UART2
        0x00,       # USB
        0x00,       # SPI
        0x00,
    ])


def cfg_save() -> bytes:
    return bytes([
        0x00, 0x00, 0x00, 0x00,  # clearMask
        0xFF, 0xFF, 0x00, 0x00,  # saveMask
        0x00, 0x00, 0x00, 0x00,  # loadMask
        0x17,                    # deviceMask
    ])


def preview_nmea(port: str, baud: int, seconds: float):
    print(f"Reading {seconds:g}s preview from {port} at {baud}bps...")
    deadline = time.monotonic() + seconds
    count = 0

    with serial.Serial(port, baud, timeout=0.5) as ser:
        while time.monotonic() < deadline:
            raw = ser.readline()
            if not raw:
                continue

            line = raw.decode("ascii", errors="replace").strip()
            if not line:
                continue

            print(line)
            count += 1

    return count


def main():
    parser = argparse.ArgumentParser(description="Configure VK2828U7G5LF/u-blox7 GPS for 10Hz NMEA at 115200bps.")
    parser.add_argument("--port", default="/dev/ttyAMA0")
    parser.add_argument("--initial-baud", type=int, default=9600)
    parser.add_argument("--target-baud", type=int, default=115200)
    parser.add_argument("--preview-sec", type=float, default=3.0)
    parser.add_argument(
        "--dynamic-model",
        choices=["none", *DYNAMIC_MODELS.keys()],
        default="automotive",
        help="navigation dynamic model",
    )
    parser.add_argument("--no-sbas", action="store_true", help="do not enable SBAS/MSAS")
    parser.add_argument("--no-save", action="store_true", help="do not save settings to module flash")
    args = parser.parse_args()

    if args.target_baud != 115200:
        raise SystemExit("This helper currently supports target baud 115200 only.")

    print(f"Opening {args.port} at {args.initial_baud}bps...")
    with serial.Serial(args.port, args.initial_baud, timeout=1) as ser:
        print("Setting UART1 to 115200bps...")
        write_ubx(ser, 0x06, 0x00, cfg_prt_uart1_115200(), delay=0.4)

    print(f"Reopening {args.port} at 115200bps...")
    with serial.Serial(args.port, 115200, timeout=1) as ser:
        print("Setting navigation rate to 10Hz...")
        write_ubx(ser, 0x06, 0x08, cfg_rate_10hz())

        if args.dynamic_model != "none":
            print(f"Setting dynamic model to {args.dynamic_model}...")
            write_ubx(ser, 0x06, 0x24, cfg_nav5_dynamic_model(args.dynamic_model))

        if not args.no_sbas:
            print("Enabling SBAS/MSAS auto-scan...")
            write_ubx(ser, 0x06, 0x16, cfg_sbas_enabled())

        print("Enabling GGA/RMC and disabling GSA/GSV/VTG/GLL on UART1...")
        for msg_id in [0x00, 0x04]:  # GGA, RMC
            write_ubx(ser, 0x06, 0x01, cfg_msg_nmea(msg_id, 1), delay=0.05)

        for msg_id in [0x02, 0x03, 0x05, 0x01]:  # GSA, GSV, VTG, GLL
            write_ubx(ser, 0x06, 0x01, cfg_msg_nmea(msg_id, 0), delay=0.05)

        if not args.no_save:
            print("Saving current configuration to flash...")
            write_ubx(ser, 0x06, 0x09, cfg_save(), delay=0.5)

    count = preview_nmea(args.port, 115200, args.preview_sec)
    if count == 0:
        print("No NMEA was read at 115200bps. Check GPS RX wiring and retry.", file=sys.stderr)
        return 1

    print(f"Done. Read {count} NMEA lines at 115200bps.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
