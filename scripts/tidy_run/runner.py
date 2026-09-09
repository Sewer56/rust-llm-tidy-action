"""Tidy binary execution with console-mirrored output capture."""

import subprocess
import sys
import threading


def capture(binary, argv, stdout_path, stderr_path):
    """Run the tidy binary, mirroring output to the console and both files.

    Returns the tool's exit status. A nonzero status is the tool's
    verdict about the tree, not a step failure, so it is returned instead
    of raised. Spawn failures return the shell's conventional status.
    """
    try:
        proc = subprocess.Popen(
            [binary, *argv], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except FileNotFoundError:
        return 127
    except PermissionError:
        return 126
    except OSError:
        return 127

    pumps = [
        threading.Thread(
            target=_pump, args=(proc.stdout, sys.stdout, stdout_path)
        ),
        threading.Thread(
            target=_pump, args=(proc.stderr, sys.stderr, stderr_path)
        ),
    ]
    for pump in pumps:
        pump.start()
    proc.wait()
    for pump in pumps:
        pump.join()
    return proc.returncode


def _pump(source, echo, sink_path):
    """Copy a binary pipe to the console stream and `sink_path`."""
    with open(sink_path, "wb") as sink:
        for chunk in iter(source.readline, b""):
            sink.write(chunk)
            echo.buffer.write(chunk)
            echo.buffer.flush()
        source.close()
