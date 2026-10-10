from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class File(_message.Message):
    __slots__ = ("path", "content")
    PATH_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    path: str
    content: bytes
    def __init__(self, path: _Optional[str] = ..., content: _Optional[bytes] = ...) -> None: ...

class Bundle(_message.Message):
    __slots__ = ("id", "files")
    ID_FIELD_NUMBER: _ClassVar[int]
    FILES_FIELD_NUMBER: _ClassVar[int]
    id: str
    files: _containers.RepeatedCompositeFieldContainer[File]
    def __init__(self, id: _Optional[str] = ..., files: _Optional[_Iterable[_Union[File, _Mapping]]] = ...) -> None: ...

class ApplyBundleRequest(_message.Message):
    __slots__ = ("bundle", "signature")
    BUNDLE_FIELD_NUMBER: _ClassVar[int]
    SIGNATURE_FIELD_NUMBER: _ClassVar[int]
    bundle: bytes
    signature: bytes
    def __init__(self, bundle: _Optional[bytes] = ..., signature: _Optional[bytes] = ...) -> None: ...

class ApplyBundleResponse(_message.Message):
    __slots__ = ("ok", "error", "changed_files", "reloaded_units", "restored_previous")
    OK_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    CHANGED_FILES_FIELD_NUMBER: _ClassVar[int]
    RELOADED_UNITS_FIELD_NUMBER: _ClassVar[int]
    RESTORED_PREVIOUS_FIELD_NUMBER: _ClassVar[int]
    ok: bool
    error: str
    changed_files: _containers.RepeatedScalarFieldContainer[str]
    reloaded_units: _containers.RepeatedScalarFieldContainer[str]
    restored_previous: bool
    def __init__(self, ok: _Optional[bool] = ..., error: _Optional[str] = ..., changed_files: _Optional[_Iterable[str]] = ..., reloaded_units: _Optional[_Iterable[str]] = ..., restored_previous: _Optional[bool] = ...) -> None: ...

class GetStatusRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class WatchRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class BgpSession(_message.Message):
    __slots__ = ("peer", "state", "remote_asn")
    PEER_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    REMOTE_ASN_FIELD_NUMBER: _ClassVar[int]
    peer: str
    state: str
    remote_asn: int
    def __init__(self, peer: _Optional[str] = ..., state: _Optional[str] = ..., remote_asn: _Optional[int] = ...) -> None: ...

class VrrpInstance(_message.Message):
    __slots__ = ("name", "state")
    NAME_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    name: str
    state: str
    def __init__(self, name: _Optional[str] = ..., state: _Optional[str] = ...) -> None: ...

class Status(_message.Message):
    __slots__ = ("agent_version", "bundle_id", "file_hashes", "bgp", "vrrp", "haproxy_healthy", "routes_withdrawn", "observed_at_unix")
    class FileHashesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    AGENT_VERSION_FIELD_NUMBER: _ClassVar[int]
    BUNDLE_ID_FIELD_NUMBER: _ClassVar[int]
    FILE_HASHES_FIELD_NUMBER: _ClassVar[int]
    BGP_FIELD_NUMBER: _ClassVar[int]
    VRRP_FIELD_NUMBER: _ClassVar[int]
    HAPROXY_HEALTHY_FIELD_NUMBER: _ClassVar[int]
    ROUTES_WITHDRAWN_FIELD_NUMBER: _ClassVar[int]
    OBSERVED_AT_UNIX_FIELD_NUMBER: _ClassVar[int]
    agent_version: str
    bundle_id: str
    file_hashes: _containers.ScalarMap[str, str]
    bgp: _containers.RepeatedCompositeFieldContainer[BgpSession]
    vrrp: _containers.RepeatedCompositeFieldContainer[VrrpInstance]
    haproxy_healthy: bool
    routes_withdrawn: bool
    observed_at_unix: int
    def __init__(self, agent_version: _Optional[str] = ..., bundle_id: _Optional[str] = ..., file_hashes: _Optional[_Mapping[str, str]] = ..., bgp: _Optional[_Iterable[_Union[BgpSession, _Mapping]]] = ..., vrrp: _Optional[_Iterable[_Union[VrrpInstance, _Mapping]]] = ..., haproxy_healthy: _Optional[bool] = ..., routes_withdrawn: _Optional[bool] = ..., observed_at_unix: _Optional[int] = ...) -> None: ...
