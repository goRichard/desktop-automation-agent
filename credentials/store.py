"""Secret storage abstraction with a native Windows Credential Manager backend."""
from __future__ import annotations

import ctypes
import sys
from abc import ABC, abstractmethod
from ctypes import wintypes
from typing import Optional


class CredentialStoreUnavailable(RuntimeError):
    pass


class SecretStore(ABC):
    @abstractmethod
    def get(self, secret_id: str) -> Optional[str]:
        raise NotImplementedError

    @abstractmethod
    def set(self, secret_id: str, value: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def delete(self, secret_id: str) -> bool:
        raise NotImplementedError


class MemorySecretStore(SecretStore):
    """Test-only store; never selected by the production factory."""

    def __init__(self):
        self._values: dict[str, str] = {}

    def get(self, secret_id: str) -> Optional[str]:
        return self._values.get(secret_id)

    def set(self, secret_id: str, value: str) -> None:
        self._values[secret_id] = value

    def delete(self, secret_id: str) -> bool:
        return self._values.pop(secret_id, None) is not None


class UnavailableSecretStore(SecretStore):
    def get(self, secret_id: str) -> Optional[str]:
        return None

    def set(self, secret_id: str, value: str) -> None:
        raise CredentialStoreUnavailable("Windows Credential Manager is unavailable")

    def delete(self, secret_id: str) -> bool:
        raise CredentialStoreUnavailable("Windows Credential Manager is unavailable")


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class WindowsCredentialStore(SecretStore):
    _TYPE_GENERIC = 1
    _PERSIST_LOCAL_MACHINE = 2
    _ERROR_NOT_FOUND = 1168

    def __init__(self, namespace: str = "SEWC.FlowPilot"):
        if sys.platform != "win32":
            raise CredentialStoreUnavailable("Windows Credential Manager requires Windows")
        self.namespace = namespace
        self._advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self._configure_functions()

    def _configure_functions(self) -> None:
        self._advapi32.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
        self._advapi32.CredWriteW.restype = wintypes.BOOL
        self._advapi32.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
        ]
        self._advapi32.CredReadW.restype = wintypes.BOOL
        self._advapi32.CredDeleteW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self._advapi32.CredDeleteW.restype = wintypes.BOOL
        self._advapi32.CredFree.argtypes = [ctypes.c_void_p]

    def get(self, secret_id: str) -> Optional[str]:
        pointer = ctypes.POINTER(_CREDENTIALW)()
        if not self._advapi32.CredReadW(
            self._target(secret_id), self._TYPE_GENERIC, 0, ctypes.byref(pointer)
        ):
            error = ctypes.get_last_error()
            if error == self._ERROR_NOT_FOUND:
                return None
            raise ctypes.WinError(error)
        try:
            credential = pointer.contents
            raw = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
            return raw.decode("utf-16-le")
        finally:
            self._advapi32.CredFree(pointer)

    def set(self, secret_id: str, value: str) -> None:
        raw = value.encode("utf-16-le")
        if len(raw) > 2560:
            raise ValueError("Credential exceeds the Windows generic credential size limit")
        blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        credential = _CREDENTIALW(
            Type=self._TYPE_GENERIC,
            TargetName=self._target(secret_id),
            CredentialBlobSize=len(raw),
            CredentialBlob=ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte)),
            Persist=self._PERSIST_LOCAL_MACHINE,
            UserName="SEWC FlowPilot",
        )
        if not self._advapi32.CredWriteW(ctypes.byref(credential), 0):
            raise ctypes.WinError(ctypes.get_last_error())

    def delete(self, secret_id: str) -> bool:
        if self._advapi32.CredDeleteW(self._target(secret_id), self._TYPE_GENERIC, 0):
            return True
        error = ctypes.get_last_error()
        if error == self._ERROR_NOT_FOUND:
            return False
        raise ctypes.WinError(error)

    def _target(self, secret_id: str) -> str:
        if not secret_id or any(character in secret_id for character in "\r\n\0"):
            raise ValueError("Invalid credential secret id")
        return f"{self.namespace}/{secret_id}"


_default_store: Optional[SecretStore] = None


def get_default_secret_store() -> SecretStore:
    global _default_store
    if _default_store is None:
        if sys.platform == "win32":
            _default_store = WindowsCredentialStore()
        else:
            _default_store = UnavailableSecretStore()
    return _default_store
