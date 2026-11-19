import os
import shutil
import subprocess
from pathlib import Path


NETWORK_FAILURE = 69
CONFIGURATION_FAILURE = 78


def executable(env_var, default):
    command = os.environ.get(env_var) or default
    path = shutil.which(command)
    if path is None:
        raise ValueError(f"Cannot find {command!r}. Set {env_var} to the native executable.")
    return str(Path(path).absolute())


def child_exit_status(returncode):
    return returncode if returncode >= 0 else 128 - returncode


def terminate_child(child, grace_seconds=1):
    if child.poll() is None:
        child.terminate()
        try:
            return child.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            child.kill()
    return child.wait()
