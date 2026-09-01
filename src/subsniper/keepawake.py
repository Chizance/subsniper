"""Stop Windows from sleeping while SubSniper is polling.

A sleeping machine is indistinguishable from a working one until you notice
the jobs you never got told about. The process is not killed; it is frozen.
No polls happen, no errors are logged, and on wake it carries on as if
nothing happened. In the audit log this looks exactly like a coverage gap,
which is why `doctor` reports gaps -- but reporting it after the fact is a
consolation prize. Better not to sleep.

`powercfg /change standby-timeout-ac 0` (documented in the README) only covers
AC power, and does nothing about the lid. The Windows-sanctioned way for a
program to say "I am doing something, stay awake" is SetThreadExecutionState,
which is what media players use so a film doesn't pause halfway through.

ES_SYSTEM_REQUIRED keeps the machine awake. ES_DISPLAY_REQUIRED is deliberately
NOT set: the screen may sleep, and should, since nobody is watching it. The
flag is process-wide and is dropped automatically when the process exits, so a
crash cannot leave the machine permanently awake.

On anything other than Windows this is a no-op that reports itself as such.
"""

from __future__ import annotations

import logging
import platform

log = logging.getLogger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def keep_system_awake() -> str:
    """Ask Windows not to sleep. Returns a human-readable status for `doctor`."""
    if platform.system() != "Windows":
        return f"not needed on {platform.system() or 'this platform'}"

    try:
        import ctypes

        result = ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        )
    except Exception as exc:  # noqa: BLE001 - never let this stop the service
        log.warning("could not request stay-awake from Windows: %s", exc)
        return f"FAILED ({exc}) - set sleep to Never in Windows power settings"

    if not result:
        log.warning("Windows refused the stay-awake request")
        return "REFUSED by Windows - set sleep to Never in power settings"

    log.info("asked Windows to stay awake while polling")
    return "active - Windows will not sleep while SubSniper runs"


def release() -> None:
    """Drop the request. Windows does this on exit anyway; this is for tests."""
    if platform.system() != "Windows":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except Exception:  # noqa: BLE001
        pass
