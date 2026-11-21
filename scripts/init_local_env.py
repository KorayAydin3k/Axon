"""Create private local development credentials without replacing an existing .env."""

import os
from pathlib import Path
import re
from secrets import token_hex, token_urlsafe


def initialize(root: Path) -> bool:
    target = root / ".env"
    if target.exists():
        return False

    template = (root / ".env.example").read_text()
    password = token_hex(24)
    values = {
        "POSTGRES_PASSWORD": password,
        "DATABASE_URL": f"postgresql+psycopg://axon:{password}@127.0.0.1:5432/axon",
        "AXON_APPROVAL_TOKEN": token_urlsafe(32),
    }
    for key, value in values.items():
        template, count = re.subn(rf"^{key}=.*$", f"{key}={value}", template, flags=re.MULTILINE)
        if count != 1:
            raise ValueError(f"Expected one {key} entry in .env.example")

    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w") as stream:
        stream.write(template)
    return True


if __name__ == "__main__":
    created = initialize(Path(__file__).resolve().parents[1])
    print("Created private .env with local credentials." if created else "Keeping existing .env unchanged.")
