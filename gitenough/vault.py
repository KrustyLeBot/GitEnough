"""PAT storage in the OS keyring (Windows Credential Manager, DPAPI-encrypted)."""

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

SERVICE = "GitEnough"
LEGACY_SERVICE = "GitTracker"  # name of the app's first release


def get_token(host: str) -> str | None:
    try:
        token = keyring.get_password(SERVICE, host)
        if token is None:
            # Tokens saved by GitTracker move to the new name on first use.
            token = keyring.get_password(LEGACY_SERVICE, host)
            if token is not None:
                keyring.set_password(SERVICE, host, token)
                keyring.delete_password(LEGACY_SERVICE, host)
        return token
    except KeyringError:
        return None


def set_token(host: str, token: str) -> None:
    keyring.set_password(SERVICE, host, token)


def delete_token(host: str) -> None:
    try:
        keyring.delete_password(SERVICE, host)
    except (PasswordDeleteError, KeyringError):
        pass


def default_username(host: str) -> str:
    # Username conventions accepted by each forge for token-based Basic auth.
    if "github" in host:
        return "x-access-token"
    if "bitbucket.org" in host:
        return "x-token-auth"
    return "oauth2"
