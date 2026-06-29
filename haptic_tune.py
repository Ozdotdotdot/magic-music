#!/usr/bin/env python3
"""
Haptic tuner -- dial in the actuator feel without running the whole daemon.

The actuator report has three tunable bytes (indices 3, 6, 11 of the 15-byte
report): b3 = strength, b6 and b11 = waveform/texture. The firmware's real
button clicks use two distinct triples:
    down-click  b3=0x3f b6=0x06 b11=0x06
    up-click    b3=0x11 b6=0x04 b11=0x04
A click "feels right" because down and up differ in ALL THREE bytes, not just
strength. Use this to find triples that feel like a real down/up pair, then put
them in /etc/magicmusic.toml.

Run with sudo (hidraw is root-only):
    sudo python3 haptic_tune.py

Commands (hex bytes, spaces optional between them):
    3f 06 06        fire one buzz with those three bytes
    3f              fire one buzz, strength only (b6/b11 keep current down vals)
    d 3f 06 06      set the DOWN triple
    u 11 04 04      set the UP triple
    <enter> / c     fire a full click: down, gap, up
    g 0.12          set the gap (seconds) between down and up in a click
    p               print current down/up/gap
    q               quit
"""
import sys
import time
from magicmusic import find_hidraw, haptic_report


def main():
    path = find_hidraw()
    if not path:
        sys.exit("no trackpad hidraw found (driver loaded? device connected?)")
    hid = open(path, "wb", buffering=0)
    print(f"hidraw: {path}")

    down = [0x3f, 0x06, 0x06]
    up = [0x11, 0x04, 0x04]
    gap = 0.12

    def fire(triple):
        hid.write(haptic_report(triple[0], triple[1], triple[2]))

    def show():
        d = " ".join(f"{b:02x}" for b in down)
        u = " ".join(f"{b:02x}" for b in up)
        print(f"  down=[{d}]  up=[{u}]  gap={gap}s")

    print("down/up = current click pair. <enter> fires the pair. 'q' quits, 'p' prints.\n")
    show()

    while True:
        try:
            line = input("haptic> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if line in ("q", "quit", "exit"):
            return
        if line in ("", "c", "click"):
            fire(down)
            time.sleep(gap)
            fire(up)
            continue
        if line == "p":
            show()
            continue

        parts = line.split()
        head = parts[0].lower()

        try:
            if head in ("d", "u"):
                vals = [int(x, 16) for x in parts[1:]]
                if len(vals) != 3:
                    print("  need 3 hex bytes, e.g. 'd 3f 06 06'")
                    continue
                (down if head == "d" else up)[:] = vals
                show()
            elif head == "g":
                gap = float(parts[1])
                show()
            elif head == "down":
                fire(down)
            elif head == "up":
                fire(up)
            else:
                vals = [int(x, 16) for x in parts]
                if len(vals) == 1:
                    fire([vals[0], down[1], down[2]])
                elif len(vals) == 3:
                    fire(vals)
                else:
                    print("  give 1 byte (strength) or 3 bytes (b3 b6 b11)")
        except (ValueError, IndexError):
            print("  parse error -- hex bytes only, e.g. '3f 06 06' or 'g 0.1'")


if __name__ == "__main__":
    main()
