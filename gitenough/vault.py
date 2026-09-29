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


# Token keys in use: a host ("gitlab.com") or a host plus group path ("gitlab.com/some-org"), for servers
# where each organization has its own account and token. Kept in line with config.hosts by the app.
SCOPES: set[str] = set()


def set_scopes(keys) -> None:
    SCOPES.clear()
    SCOPES.update(k.lower().strip("/") for k in keys if k)


def scope_for(host: str, path: str = "") -> str:
    """Most specific token key for a project path: the deepest matching group, else the host."""
    host = host.lower()
    parts = [p for p in path.strip("/").split("/") if p]
    for i in range(len(parts), 0, -1):
        key = host + "/" + "/".join(parts[:i]).lower()
        if key in SCOPES:
            return key
    return host


def token_for(host: str, path: str = "") -> tuple[str, str | None]:
    """(key, token) for a project: its group's token when one is saved, else the host's."""
    key = scope_for(host, path)
    token = get_token(key)
    if token is None and key != host.lower():
        key, token = host.lower(), get_token(host.lower())
    return key, token


def host_of(key: str) -> str:
    return key.split("/", 1)[0]


def keys_of_host(host: str) -> list[str]:
    """Every token key of a server (its host key and its group keys)."""
    host = host.lower()
    return sorted({k for k in SCOPES if host_of(k) == host} | {host})


def default_username(host: str) -> str:
    # Username conventions accepted by each forge for token-based Basic auth.
    host = host_of(host)
    if "github" in host:
        return "x-access-token"
    if "bitbucket.org" in host:
        return "x-token-auth"
    return "oauth2"
