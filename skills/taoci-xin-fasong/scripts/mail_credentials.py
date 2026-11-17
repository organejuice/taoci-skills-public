"""Windows Credential Manager binding for one configured outreach sender.

Only the exact project target is accessed. No credential enumeration, files,
environment secrets, client database access or third-party dependency is used.
Based on Microsoft's public CREDENTIALW / CredReadW / CredWriteW API.
"""
from __future__ import annotations

import ctypes
import getpass
import os
import re
from ctypes import wintypes

TARGET_PREFIX = "Codex/Outreach-Public/"


class CredentialError(ValueError):
    pass


def target_name(account: str) -> str:
    if not isinstance(account, str) or not re.fullmatch(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}", account):
        raise CredentialError("Enter one bare sender email address; whitespace and header injection are rejected")
    return TARGET_PREFIX + account


class CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class WindowsCredentialStore:
    def __init__(self) -> None:
        if os.name != "nt":
            raise CredentialError("Windows Credential Manager is available only on Windows")
        self.api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self.api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                      wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
        self.api.CredReadW.restype = wintypes.BOOL
        self.api.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
        self.api.CredWriteW.restype = wintypes.BOOL
        self.api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self.api.CredDeleteW.restype = wintypes.BOOL
        self.api.CredFree.argtypes = [ctypes.c_void_p]
        self.api.CredFree.restype = None

    def read(self, account: str) -> str | None:
        target = target_name(account)
        pointer = ctypes.POINTER(CREDENTIALW)()
        if not self.api.CredReadW(target, 1, 0, ctypes.byref(pointer)):
            code = ctypes.get_last_error()
            if code == 1168:  # ERROR_NOT_FOUND
                return None
            raise CredentialError(f"Credential Manager read failed (Windows error {code})")
        try:
            value = pointer.contents
            if value.Type != 1 or value.UserName != account or value.CredentialBlobSize % 2:
                raise CredentialError("The project credential has unexpected metadata")
            raw = ctypes.string_at(value.CredentialBlob, value.CredentialBlobSize)
            return raw.decode("utf-16-le")
        finally:
            self.api.CredFree(pointer)

    def write(self, account: str, secret: str, *, replace: bool = False) -> None:
        target = target_name(account)
        if not isinstance(secret, str) or not 8 <= len(secret) <= 1024 or any(c in secret for c in "\r\n\x00"):
            raise CredentialError("Enter a provider-issued authorization secret of 8 to 1024 characters")
        if self.read(account) is not None and not replace:
            raise CredentialError("The project credential already exists; use --replace for an intentional update")
        raw = secret.encode("utf-16-le")
        buffer = ctypes.create_string_buffer(raw)
        value = CREDENTIALW()
        value.Type = 1  # CRED_TYPE_GENERIC
        value.TargetName = target
        value.Comment = "Configured outreach SMTP/IMAP client authorization secret"
        value.UserName = account
        value.CredentialBlobSize = len(raw)
        value.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        value.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE: this Windows user on this machine
        try:
            if not self.api.CredWriteW(ctypes.byref(value), 0):
                raise CredentialError(f"Credential Manager write failed (Windows error {ctypes.get_last_error()})")
        finally:
            ctypes.memset(ctypes.addressof(buffer), 0, len(buffer))

    def delete(self, account: str) -> bool:
        if not self.api.CredDeleteW(target_name(account), 1, 0):
            code = ctypes.get_last_error()
            if code == 1168:
                return False
            raise CredentialError(f"Credential Manager delete failed (Windows error {code})")
        return True


def prompt_and_store(account: str, *, replace: bool = False, store=None) -> None:
    # Running this in the user's local terminal keeps the code out of chat,
    # command-line arguments, environment variables and project artifacts.
    import sys
    if not sys.stdin.isatty():
        raise CredentialError("Run auth set in your own interactive terminal; no code is accepted from a pipe or command argument")
    vault = store or WindowsCredentialStore()
    if vault.read(account) is not None and not replace:
        raise CredentialError("The project credential already exists; use --replace to update")
    secret = getpass.getpass("Mail client authorization secret (hidden): ")
    repeat = getpass.getpass("Repeat authorization code (hidden): ")
    if secret != repeat:
        raise CredentialError("Authorization codes did not match; no credential was written")
    vault.write(account, secret, replace=replace)
