"""macOS Keychain utilities for secure secret storage."""

import subprocess

SERVICE_NAME = "portfolio-tracker"


def get_secret(key: str) -> str | None:
    """Read a secret from macOS Keychain.

    Args:
        key: The account name (e.g. DEBANK_API_KEY)

    Returns:
        The secret value, or None if not found.
    """
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", SERVICE_NAME, "-a", key, "-w"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except FileNotFoundError:
        pass
    return None


def set_secret(key: str, value: str) -> bool:
    """Store a secret in macOS Keychain.

    Args:
        key: The account name (e.g. DEBANK_API_KEY)
        value: The secret value

    Returns:
        True if successful.
    """
    result = subprocess.run(
        ["security", "add-generic-password", "-s", SERVICE_NAME, "-a", key, "-w", value, "-U"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def delete_secret(key: str) -> bool:
    """Delete a secret from macOS Keychain."""
    result = subprocess.run(
        ["security", "delete-generic-password", "-s", SERVICE_NAME, "-a", key],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0
