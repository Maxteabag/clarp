"""The OpenAI key for the GPT-Live lab scripts (issue 473): taken only from an environment
variable whose name starts with OPENAI_, so a slip of the hand cannot send another secret to
the provider as the bearer. The name is checked when the arguments are read, before any
variable is read and before anything is called."""
from __future__ import annotations

import argparse
import os
import re
import sys

PREFIX = "OPENAI_"


def key_env_name(name):
    """argparse type for --key-env: only an OPENAI_ name; refused in words otherwise."""
    if not re.fullmatch(r"OPENAI_[A-Za-z0-9_]+", name or ""):
        raise argparse.ArgumentTypeError(
            f"refused: {name!r} is not an OpenAI key variable; --key-env takes only a name that starts with "
            f"{PREFIX} (for example OPENAI_API_KEY), so no other secret can be sent to the provider")
    return name


def key_from(env):
    key_env_name(env)                     # again here: the only place a variable is read
    value = os.environ.get(env, "")
    if not value.strip():
        sys.exit(f"Set the OpenAI key in the environment variable {env} (it is read from there only).")
    return value.strip()


def scrub(text, key):
    return str(text).replace(key, "[key]") if key else str(text)
