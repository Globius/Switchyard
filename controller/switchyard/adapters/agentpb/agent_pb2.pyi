from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class CaptureRequest(_message.Message):
    __slots__ = ("interface", "protocol", "host", "port", "duration_s", "max_packets", "snaplen")
    INTERFACE_FIELD_NUMBER: _ClassVar[int]
    PROTOCOL_FIELD_NUMBER: _ClassVar[int]
    HOST_FIELD_NUMBER: _ClassVar[int]
    PORT_FIELD_NUMBER: _ClassVar[int]
    DURATION_S_FIELD_NUMBER: _ClassVar[int]
    MAX_PACKETS_FIELD_NUMBER: _ClassVar[int]
    SNAPLEN_FIELD_NUMBER: _ClassVar[int]
    interface: str
    protocol: str
    host: str
    port: int
    duration_s: int
    max_packets: int
    snaplen: int
    def __init__(self, interface: _Optional[str] = ..., protocol: _Optional[str] = ..., host: _Optional[str] = ..., port: _Optional[int] = ..., duration_s: _Optional[int] = ..., max_packets: _Optional[int] = ..., snaplen: _Optional[int] = ...) -> None: ...

class CaptureChunk(_message.Message):
    __slots__ = ("data", "done", "packets", "truncated", "error")
    DATA_FIELD_NUMBER: _ClassVar[int]
    DONE_FIELD_NUMBER: _ClassVar[int]
    PACKETS_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    data: bytes
    done: bool
    packets: int
    truncated: bool
    error: str
    def __init__(self, data: _Optional[bytes] = ..., done: _Optional[bool] = ..., packets: _Optional[int] = ..., truncated: _Optional[bool] = ..., error: _Optional[str] = ...) -> None: ...

class ProbeRequest(_message.Message):
    __slots__ = ("protocol", "address", "port", "timeout_ms", "http_method", "http_path", "http_host", "expect_status", "udp_payload", "udp_expect")
    PROTOCOL_FIELD_NUMBER: _ClassVar[int]
    ADDRESS_FIELD_NUMBER: _ClassVar[int]
    PORT_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    HTTP_METHOD_FIELD_NUMBER: _ClassVar[int]
    HTTP_PATH_FIELD_NUMBER: _ClassVar[int]
    HTTP_HOST_FIELD_NUMBER: _ClassVar[int]
    EXPECT_STATUS_FIELD_NUMBER: _ClassVar[int]
    UDP_PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    UDP_EXPECT_FIELD_NUMBER: _ClassVar[int]
    protocol: str
    address: str
    port: int
    timeout_ms: int
    http_method: str
    http_path: str
    http_host: str
    expect_status: _containers.RepeatedScalarFieldContainer[int]
    udp_payload: bytes
    udp_expect: bytes
    def __init__(self, protocol: _Optional[str] = ..., address: _Optional[str] = ..., port: _Optional[int] = ..., timeout_ms: _Optional[int] = ..., http_method: _Optional[str] = ..., http_path: _Optional[str] = ..., http_host: _Optional[str] = ..., expect_status: _Optional[_Iterable[int]] = ..., udp_payload: _Optional[bytes] = ..., udp_expect: _Optional[bytes] = ...) -> None: ...

class ProbeResponse(_message.Message):
    __slots__ = ("ok", "latency_ms", "detail", "refused")
    OK_FIELD_NUMBER: _ClassVar[int]
    LATENCY_MS_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    REFUSED_FIELD_NUMBER: _ClassVar[int]
    ok: bool
    latency_ms: int
    detail: str
    refused: bool
    def __init__(self, ok: _Optional[bool] = ..., latency_ms: _Optional[int] = ..., detail: _Optional[str] = ..., refused: _Optional[bool] = ...) -> None: ...

class File(_message.Message):
    __slots__ = ("path", "content")
    PATH_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    path: str
    content: bytes
    def __init__(self, path: _Optional[str] = ..., content: _Optional[bytes] = ...) -> None: ...

class Bundle(_message.Message):
    __slots__ = ("id", "files", "engine", "sequence")
    ID_FIELD_NUMBER: _ClassVar[int]
    FILES_FIELD_NUMBER: _ClassVar[int]
    ENGINE_FIELD_NUMBER: _ClassVar[int]
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    id: str
    files: _containers.RepeatedCompositeFieldContainer[File]
    engine: str
    sequence: int
    def __init__(self, id: _Optional[str] = ..., files: _Optional[_Iterable[_Union[File, _Mapping]]] = ..., engine: _Optional[str] = ..., sequence: _Optional[int] = ...) -> None: ...

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

class IpvsRealServer(_message.Message):
    __slots__ = ("service", "vip", "port", "address", "member_port", "state", "weight", "active_conns", "inactive_conns", "in_packets", "out_packets", "in_bytes", "out_bytes")
    SERVICE_FIELD_NUMBER: _ClassVar[int]
    VIP_FIELD_NUMBER: _ClassVar[int]
    PORT_FIELD_NUMBER: _ClassVar[int]
    ADDRESS_FIELD_NUMBER: _ClassVar[int]
    MEMBER_PORT_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    WEIGHT_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_CONNS_FIELD_NUMBER: _ClassVar[int]
    INACTIVE_CONNS_FIELD_NUMBER: _ClassVar[int]
    IN_PACKETS_FIELD_NUMBER: _ClassVar[int]
    OUT_PACKETS_FIELD_NUMBER: _ClassVar[int]
    IN_BYTES_FIELD_NUMBER: _ClassVar[int]
    OUT_BYTES_FIELD_NUMBER: _ClassVar[int]
    service: str
    vip: str
    port: int
    address: str
    member_port: int
    state: str
    weight: int
    active_conns: int
    inactive_conns: int
    in_packets: int
    out_packets: int
    in_bytes: int
    out_bytes: int
    def __init__(self, service: _Optional[str] = ..., vip: _Optional[str] = ..., port: _Optional[int] = ..., address: _Optional[str] = ..., member_port: _Optional[int] = ..., state: _Optional[str] = ..., weight: _Optional[int] = ..., active_conns: _Optional[int] = ..., inactive_conns: _Optional[int] = ..., in_packets: _Optional[int] = ..., out_packets: _Optional[int] = ..., in_bytes: _Optional[int] = ..., out_bytes: _Optional[int] = ...) -> None: ...

class Status(_message.Message):
    __slots__ = ("agent_version", "bundle_id", "file_hashes", "bgp", "vrrp", "haproxy_healthy", "routes_withdrawn", "observed_at_unix", "ipvs", "keepalived_running", "withdrawn_prefixes", "waf_expected", "waf_running")
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
    IPVS_FIELD_NUMBER: _ClassVar[int]
    KEEPALIVED_RUNNING_FIELD_NUMBER: _ClassVar[int]
    WITHDRAWN_PREFIXES_FIELD_NUMBER: _ClassVar[int]
    WAF_EXPECTED_FIELD_NUMBER: _ClassVar[int]
    WAF_RUNNING_FIELD_NUMBER: _ClassVar[int]
    agent_version: str
    bundle_id: str
    file_hashes: _containers.ScalarMap[str, str]
    bgp: _containers.RepeatedCompositeFieldContainer[BgpSession]
    vrrp: _containers.RepeatedCompositeFieldContainer[VrrpInstance]
    haproxy_healthy: bool
    routes_withdrawn: bool
    observed_at_unix: int
    ipvs: _containers.RepeatedCompositeFieldContainer[IpvsRealServer]
    keepalived_running: bool
    withdrawn_prefixes: _containers.RepeatedScalarFieldContainer[str]
    waf_expected: bool
    waf_running: bool
    def __init__(self, agent_version: _Optional[str] = ..., bundle_id: _Optional[str] = ..., file_hashes: _Optional[_Mapping[str, str]] = ..., bgp: _Optional[_Iterable[_Union[BgpSession, _Mapping]]] = ..., vrrp: _Optional[_Iterable[_Union[VrrpInstance, _Mapping]]] = ..., haproxy_healthy: _Optional[bool] = ..., routes_withdrawn: _Optional[bool] = ..., observed_at_unix: _Optional[int] = ..., ipvs: _Optional[_Iterable[_Union[IpvsRealServer, _Mapping]]] = ..., keepalived_running: _Optional[bool] = ..., withdrawn_prefixes: _Optional[_Iterable[str]] = ..., waf_expected: _Optional[bool] = ..., waf_running: _Optional[bool] = ...) -> None: ...
