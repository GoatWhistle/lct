"""Optional, local-only Active Directory / LDAP authentication.

Passwords are sent only over LDAPS or LDAP+StartTLS. Application roles and DDS
services are derived from administrator-configured directory group DNs; an
unmapped or ambiguously mapped account is denied instead of receiving a
default privilege.
"""

from __future__ import annotations

import asyncio
import ssl
from dataclasses import dataclass
from urllib.parse import urlparse

from app.config import Settings, get_settings
from app.domain.roles import Role


class DirectoryUnavailable(Exception):
    """The configured directory could not be reached or is misconfigured."""


class DirectoryDenied(Exception):
    """Credentials or required group mappings were not accepted."""


@dataclass(frozen=True)
class DirectoryIdentity:
    login: str
    full_name: str
    role: Role
    service: str | None
    subject: str


def map_groups(
    groups: list[str],
    role_groups: dict[str, str],
    service_groups: dict[str, str],
) -> tuple[Role, str | None]:
    normalized = {group.strip().casefold() for group in groups}
    try:
        roles = {
            Role(role)
            for group, role in role_groups.items()
            if group.strip().casefold() in normalized
        }
    except ValueError as exc:
        raise DirectoryUnavailable(
            "LDAP role group has an invalid application role"
        ) from exc
    if len(roles) != 1:
        raise DirectoryDenied("directory role mapping is missing or ambiguous")
    services = {
        service
        for group, service in service_groups.items()
        if group.strip().casefold() in normalized
    }
    if len(services) > 1:
        raise DirectoryDenied("directory service mapping is ambiguous")
    role = next(iter(roles))
    service = next(iter(services)) if services else None
    if role is not Role.TRAINEE and service is not None:
        raise DirectoryDenied("DDS service mapping is only valid for trainees")
    return role, service


def _configuration(settings: Settings):
    parsed = urlparse(settings.ldap_url)
    if (
        parsed.scheme not in {"ldap", "ldaps"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise DirectoryUnavailable("LDAP URL must use ldap:// or ldaps://")
    if (
        not settings.ldap_base_dn
        or not settings.ldap_bind_dn
        or not settings.ldap_bind_password
    ):
        raise DirectoryUnavailable(
            "LDAP base DN and service bind credentials are required"
        )
    if not settings.ldap_role_groups:
        raise DirectoryUnavailable("LDAP role group mapping is required")
    if not settings.ldap_login_attribute.replace("-", "").isalnum():
        raise DirectoryUnavailable("LDAP login attribute is invalid")
    if "{login}" in settings.ldap_user_filter:
        raise DirectoryUnavailable("do not interpolate login into LDAP_USER_FILTER")
    return parsed


def _authenticate_sync(
    login: str, password: str, settings: Settings
) -> DirectoryIdentity | None:
    """Search AD by account name, then verify the found DN with a user bind.

    `None` means no such directory account, so the HTTP layer may try an
    explicitly local account. Bad password/mapping is denied, and an outage is
    not treated as permission to fall back to a local password.
    """
    parsed = _configuration(settings)
    try:
        from ldap3 import NONE, Connection, Server, Tls
        from ldap3.core.exceptions import LDAPException
        from ldap3.utils.conv import escape_filter_chars
    except ImportError as exc:
        raise DirectoryUnavailable("LDAP support dependency is not installed") from exc

    connection = None
    try:
        tls = Tls(
            validate=ssl.CERT_REQUIRED,
            ca_certs_file=settings.ldap_ca_certs_file or None,
        )
        server = Server(
            parsed.hostname,
            port=parsed.port or (636 if parsed.scheme == "ldaps" else 389),
            use_ssl=parsed.scheme == "ldaps",
            tls=tls,
            get_info=NONE,
            connect_timeout=settings.ldap_connect_timeout_seconds,
        )
        connection = Connection(
            server,
            user=settings.ldap_bind_dn,
            password=settings.ldap_bind_password,
            auto_bind=False,
            receive_timeout=settings.ldap_connect_timeout_seconds,
            auto_referrals=False,
        )
        if not connection.open():
            raise DirectoryUnavailable("LDAP connection could not be opened")
        if parsed.scheme == "ldap" and not connection.start_tls():
            raise DirectoryUnavailable("LDAP StartTLS negotiation failed")
        if not connection.bind():
            raise DirectoryUnavailable("LDAP service bind failed")

        search_filter = (
            f"(&{settings.ldap_user_filter}"
            f"({settings.ldap_login_attribute}={escape_filter_chars(login)})"
            ")"
        )
        searched = connection.search(
            search_base=settings.ldap_base_dn,
            search_filter=search_filter,
            attributes=[
                settings.ldap_login_attribute,
                "displayName",
                "memberOf",
                "objectGUID",
                "entryUUID",
            ],
            size_limit=2,
        )
        if not searched:
            raise DirectoryUnavailable("LDAP user search failed")
        if len(connection.entries) == 0:
            return None
        if len(connection.entries) != 1:
            raise DirectoryUnavailable("LDAP login matched multiple directory entries")

        entry = connection.entries[0]
        user_dn = entry.entry_dn
        login_attribute = getattr(entry, settings.ldap_login_attribute, None)
        entry_login = (
            str(login_attribute.value or "").strip() if login_attribute else ""
        )
        if not entry_login or len(entry_login) > 80:
            raise DirectoryUnavailable("LDAP account has no usable login attribute")
        groups_value = getattr(entry, "memberOf", None)
        groups = (
            [str(value) for value in (groups_value.values or [])]
            if groups_value
            else []
        )
        object_guid = getattr(entry, "objectGUID", None)
        entry_uuid = getattr(entry, "entryUUID", None)
        raw_subject = (object_guid.value if object_guid else None) or (
            entry_uuid.value if entry_uuid else None
        )
        if raw_subject is None or raw_subject == "":
            raise DirectoryUnavailable(
                "LDAP account must expose objectGUID or entryUUID"
            )
        if isinstance(raw_subject, bytes):
            if len(raw_subject) == 16:
                from uuid import UUID

                subject = str(UUID(bytes_le=raw_subject))
            else:
                subject = raw_subject.decode("utf-8", errors="strict").strip()
        else:
            subject = str(raw_subject).strip()
        if not subject or len(subject) > 256:
            raise DirectoryUnavailable(
                "LDAP account must expose objectGUID or entryUUID"
            )
        display_name = getattr(entry, "displayName", None)
        full_name = (
            str(display_name.value or entry_login).strip()[:120]
            if display_name
            else entry_login
        )

        connection.rebind(user=user_dn, password=password)
        if not connection.bound:
            raise DirectoryDenied("invalid directory credentials")
        role, service = map_groups(
            groups, settings.ldap_role_groups, settings.ldap_service_groups
        )
        return DirectoryIdentity(
            login=entry_login.casefold(),
            full_name=full_name or entry_login,
            role=role,
            service=service,
            subject=subject,
        )
    except DirectoryDenied:
        raise
    except DirectoryUnavailable:
        raise
    except LDAPException as exc:
        # Do not leak DN, server internals, or credentials to the HTTP client.
        result = getattr(connection, "result", {}) if connection is not None else {}
        if result.get("result") == 49:
            raise DirectoryDenied("invalid directory credentials") from exc
        raise DirectoryUnavailable("directory authentication failed") from exc
    except (OSError, ssl.SSLError, TimeoutError, ValueError) as exc:
        raise DirectoryUnavailable("directory service unavailable") from exc
    finally:
        if connection is not None:
            try:
                connection.unbind()
            except (LDAPException, OSError):
                pass


async def authenticate(login: str, password: str) -> DirectoryIdentity | None:
    settings = get_settings()
    return await asyncio.to_thread(_authenticate_sync, login, password, settings)
