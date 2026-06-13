#!/usr/bin/env python3
"""
Magic Music — Phase 2 (COSMIC edition).

On COSMIC, 3-finger gestures are unclaimed (no scroll, no workspace — that's
4-finger), so we read the trackpad PASSIVELY. No EVIOCGRAB, no virtual device,
no phantom-touch bugs.

Controls:
  - 3 fingers down            -> "ready" buzz (you're entering volume mode)
  - 3-finger vertical slide   -> volume, haptic tick per step (up = louder)
  - 1-finger force-click >180 -> play/pause (`mpc toggle`). No movement check,
                                 so a hard press never needs to hold still.

Run with sudo (needs raw access to the evdev + hidraw nodes):
    sudo python3 magicmusic.py
"""
import glob
import os
import select
import subprocess
import time
import evdev
from evdev import ecodes

# --- tunables -------------------------------------------------------------
FORCE_CLICK = 180          # 1-finger pressure to fire play/pause (0-253)
PRESSURE_REARM = 120       # pressure must fall below this to re-arm play/pause
STEP_DISTANCE = 90         # *** the knob you asked for *** trackpad units of finger
                           # travel between each volume notch (one haptic buzz + one
                           # VOL_DELTA_PCT change). Higher = actuations farther apart /
                           # volume changes more slowly. Pad Y span ~5000u (printed at start).
VOL_DELTA_PCT = 2          # volume change per notch (percent). Pair with STEP_DISTANCE:
                           # raise both together to keep sensitivity but space out buzzes.
VOLUME_DEADZONE = 200      # Y units of slide to ignore after 3 fingers land
READY_DEBOUNCE = 0.04      # seconds 3 fingers must persist before the ready buzz
                           # (filters the 3-finger transient of a 4-finger swipe)
SINK = "@DEFAULT_AUDIO_SINK@"

# the daemon runs as root (for raw hidraw/evdev), but wpctl needs to reach the
# invoking user's PipeWire session — so drop to that uid for the volume call
USER_UID = int(os.environ.get("SUDO_UID", 1000))
USER_GID = int(os.environ.get("SUDO_GID", USER_UID))
USER_ENV = {"XDG_RUNTIME_DIR": f"/run/user/{USER_UID}", "PATH": "/usr/bin:/bin"}


def _drop_to_user():
    os.setgid(USER_GID)
    os.setuid(USER_UID)


def get_volume():
    """Current sink volume as a 0-1 float (read once when volume mode engages)."""
    out = subprocess.run(
        ["wpctl", "get-volume", SINK], env=USER_ENV, preexec_fn=_drop_to_user,
        capture_output=True, text=True,
    )
    try:
        return float(out.stdout.split()[1])   # "Volume: 0.78"
    except (IndexError, ValueError):
        return 0.5


_vol_procs = []

def set_volume_abs(level):
    """Set absolute sink volume, non-blocking. Absolute => no lost-increment race,
    stale calls self-correct, and the loop never stalls waiting on wpctl."""
    global _vol_procs
    _vol_procs = [p for p in _vol_procs if p.poll() is None]   # reap finished
    _vol_procs.append(subprocess.Popen(
        ["wpctl", "set-volume", "-l", "1.0", SINK, f"{level:.3f}"],
        env=USER_ENV, preexec_fn=_drop_to_user,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ))

READY_BUZZ = 0x2a          # 3 fingers landed -> volume mode ready
TICK_BUZZ = 0x12           # light per-volume-step tick
TAP_BUZZ = 0x3f            # strong play/pause confirm
# -------------------------------------------------------------------------


def find_trackpad_event():
    for path in evdev.list_devices():
        dev = evdev.InputDevice(path)
        abs_codes = {c for c, _ in dev.capabilities().get(ecodes.EV_ABS, [])}
        if "Magic Trackpad" in dev.name and ecodes.ABS_MT_PRESSURE in abs_codes:
            return dev
    raise SystemExit("Magic Trackpad event node not found (is the driver loaded?)")


def find_hidraw():
    for sysdir in glob.glob("/sys/class/hidraw/hidraw*"):
        try:
            with open(f"{sysdir}/device/uevent") as f:
                if "0265" in f.read():
                    return "/dev/" + sysdir.rsplit("/", 1)[1]
        except OSError:
            continue
    raise SystemExit("Trackpad hidraw node not found")


def haptic_report(b3, b6=0x06, b11=0x06):
    return bytes([0xF2, 0x53, 0x01, b3, 0x78, 0x02, b6, 0x24, 0x30, 0x06, 0x01, b11, 0x18, 0x48, 0x12])


def run(*cmd):
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    dev = find_trackpad_event()
    hid = open(find_hidraw(), "wb", buffering=0)
    yinfo = dev.absinfo(ecodes.ABS_Y)
    step_units = max(1, STEP_DISTANCE)
    print(f"trackpad: {dev.name} @ {dev.path}")
    notches = (yinfo.max - yinfo.min) // step_units
    print(f"Y range : {yinfo.min}..{yinfo.max}  ({step_units}u/notch -> "
          f"~{notches} notches -> ~{notches * VOL_DELTA_PCT}% volume swept across the full pad)")
    print(f"3 fingers = volume slide, 1-finger force-click ({FORCE_CLICK}) = play/pause. Ctrl-C to quit.\n")

    def buzz(strength):
        hid.write(haptic_report(strength))

    # raw state, updated per event
    cur_slot = 0
    active = set()      # MT slots currently holding a finger
    pressure = 0
    y = 0
    # decision state, evaluated per SYN frame
    volume_latched = False   # set after 3 fingers persist past the debounce
    pending_deadline = None  # monotonic time at which a tentative 3-finger buzz fires
    anchor_y = 0             # finger Y the volume slide is measured from
    anchor_vol = 0.0         # system volume (0-1) when volume engaged
    last_step = 0
    slide_armed = False      # have we moved past the one-time dead zone yet?
    pp_armed = True

    def engage_volume():
        nonlocal volume_latched, anchor_y, anchor_vol, last_step, slide_armed
        volume_latched, anchor_y, last_step, slide_armed = True, y, 0, False
        buzz(READY_BUZZ)           # instant feedback first
        anchor_vol = get_volume()  # then read the slider's starting point
        print(f"3 fingers -> volume ready (from {anchor_vol:.0%})")

    while True:
        # wake on input, or when a pending ready-buzz is due to fire
        timeout = None if pending_deadline is None else max(0.0, pending_deadline - time.monotonic())
        ready = select.select([dev.fd], [], [], timeout)[0]

        # debounce fired with three fingers still down -> engage now
        if pending_deadline is not None and time.monotonic() >= pending_deadline:
            pending_deadline = None
            if len(active) == 3 and not volume_latched:
                engage_volume()

        if not ready:
            continue

        for e in dev.read():
            if e.type == ecodes.EV_ABS:
                if e.code == ecodes.ABS_MT_SLOT:
                    cur_slot = e.value
                elif e.code == ecodes.ABS_MT_TRACKING_ID:
                    active.discard(cur_slot) if e.value == -1 else active.add(cur_slot)
                elif e.code in (ecodes.ABS_PRESSURE, ecodes.ABS_MT_PRESSURE):
                    pressure = e.value
                elif e.code == ecodes.ABS_Y:
                    y = e.value
                continue

            if not (e.type == ecodes.EV_SYN and e.code == ecodes.SYN_REPORT):
                continue

            # --- one decision per frame ---
            fingers = len(active)

            # full lift resets the latch (so the ready buzz fires once per touch,
            # immune to 3->2->3 finger-count flicker while fingers settle)
            if fingers == 0 and volume_latched:
                print(f"  volume done ({last_step:+d} steps)")
                volume_latched = False

            # tentatively start the debounce on 3 fingers; cancel it the instant
            # the count isn't 3 (e.g. a 4th finger lands -> workspace swipe)
            if fingers == 3 and not volume_latched:
                if pending_deadline is None:
                    pending_deadline = time.monotonic() + READY_DEBOUNCE
            else:
                pending_deadline = None

            # volume: absolute target from how far you've slid off the anchor
            # (up = louder; ABS_Y grows downward). The dead zone is a one-time gate:
            # ignore the first VOLUME_DEADZONE of travel (so a tap won't nudge it),
            # then re-anchor and track 1:1 with no sticky band around the anchor.
            if volume_latched and fingers == 3:
                if not slide_armed and abs(anchor_y - y) > VOLUME_DEADZONE:
                    slide_armed = True
                    anchor_y = y          # re-anchor; volume stays at anchor_vol here
                if slide_armed:
                    step = (anchor_y - y) // step_units
                    if step != last_step:
                        target = min(1.0, max(0.0, anchor_vol + step * VOL_DELTA_PCT / 100))
                        set_volume_abs(target)
                        buzz(TICK_BUZZ)
                        last_step = step

            # play/pause: 1-finger force-click, no movement check
            if pressure <= PRESSURE_REARM:
                pp_armed = True
            elif pp_armed and fingers == 1 and not volume_latched and pressure >= FORCE_CLICK:
                pp_armed = False
                run("mpc", "toggle")
                buzz(TAP_BUZZ)
                print(f"force-click (p={pressure}) -> play/pause")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nbye")
