from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class PingRequest(_message.Message):
    __slots__ = ("data",)
    DATA_FIELD_NUMBER: _ClassVar[int]
    data: bytes
    def __init__(self, data: _Optional[bytes] = ...) -> None: ...

class PingResponse(_message.Message):
    __slots__ = ("data",)
    DATA_FIELD_NUMBER: _ClassVar[int]
    data: bytes
    def __init__(self, data: _Optional[bytes] = ...) -> None: ...

class RebootRequest(_message.Message):
    __slots__ = ("mode",)
    class RebootMode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        OS: _ClassVar[RebootRequest.RebootMode]
        DFU: _ClassVar[RebootRequest.RebootMode]
        UPDATE: _ClassVar[RebootRequest.RebootMode]
    OS: RebootRequest.RebootMode
    DFU: RebootRequest.RebootMode
    UPDATE: RebootRequest.RebootMode
    MODE_FIELD_NUMBER: _ClassVar[int]
    mode: RebootRequest.RebootMode
    def __init__(self, mode: _Optional[_Union[RebootRequest.RebootMode, str]] = ...) -> None: ...

class DeviceInfoRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class DeviceInfoResponse(_message.Message):
    __slots__ = ("key", "value")
    KEY_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    key: str
    value: str
    def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...

class FactoryResetRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class GetDateTimeRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class GetDateTimeResponse(_message.Message):
    __slots__ = ("datetime",)
    DATETIME_FIELD_NUMBER: _ClassVar[int]
    datetime: DateTime
    def __init__(self, datetime: _Optional[_Union[DateTime, _Mapping]] = ...) -> None: ...

class SetDateTimeRequest(_message.Message):
    __slots__ = ("datetime",)
    DATETIME_FIELD_NUMBER: _ClassVar[int]
    datetime: DateTime
    def __init__(self, datetime: _Optional[_Union[DateTime, _Mapping]] = ...) -> None: ...

class DateTime(_message.Message):
    __slots__ = ("hour", "minute", "second", "day", "month", "year", "weekday")
    HOUR_FIELD_NUMBER: _ClassVar[int]
    MINUTE_FIELD_NUMBER: _ClassVar[int]
    SECOND_FIELD_NUMBER: _ClassVar[int]
    DAY_FIELD_NUMBER: _ClassVar[int]
    MONTH_FIELD_NUMBER: _ClassVar[int]
    YEAR_FIELD_NUMBER: _ClassVar[int]
    WEEKDAY_FIELD_NUMBER: _ClassVar[int]
    hour: int
    minute: int
    second: int
    day: int
    month: int
    year: int
    weekday: int
    def __init__(self, hour: _Optional[int] = ..., minute: _Optional[int] = ..., second: _Optional[int] = ..., day: _Optional[int] = ..., month: _Optional[int] = ..., year: _Optional[int] = ..., weekday: _Optional[int] = ...) -> None: ...

class PlayAudiovisualAlertRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ProtobufVersionRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ProtobufVersionResponse(_message.Message):
    __slots__ = ("major", "minor")
    MAJOR_FIELD_NUMBER: _ClassVar[int]
    MINOR_FIELD_NUMBER: _ClassVar[int]
    major: int
    minor: int
    def __init__(self, major: _Optional[int] = ..., minor: _Optional[int] = ...) -> None: ...

class UpdateRequest(_message.Message):
    __slots__ = ("update_manifest",)
    UPDATE_MANIFEST_FIELD_NUMBER: _ClassVar[int]
    update_manifest: str
    def __init__(self, update_manifest: _Optional[str] = ...) -> None: ...

class UpdateResponse(_message.Message):
    __slots__ = ("code",)
    class UpdateResultCode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        OK: _ClassVar[UpdateResponse.UpdateResultCode]
        ManifestPathInvalid: _ClassVar[UpdateResponse.UpdateResultCode]
        ManifestFolderNotFound: _ClassVar[UpdateResponse.UpdateResultCode]
        ManifestInvalid: _ClassVar[UpdateResponse.UpdateResultCode]
        StageMissing: _ClassVar[UpdateResponse.UpdateResultCode]
        StageIntegrityError: _ClassVar[UpdateResponse.UpdateResultCode]
        ManifestPointerError: _ClassVar[UpdateResponse.UpdateResultCode]
        TargetMismatch: _ClassVar[UpdateResponse.UpdateResultCode]
        OutdatedManifestVersion: _ClassVar[UpdateResponse.UpdateResultCode]
        IntFull: _ClassVar[UpdateResponse.UpdateResultCode]
        UnspecifiedError: _ClassVar[UpdateResponse.UpdateResultCode]
    OK: UpdateResponse.UpdateResultCode
    ManifestPathInvalid: UpdateResponse.UpdateResultCode
    ManifestFolderNotFound: UpdateResponse.UpdateResultCode
    ManifestInvalid: UpdateResponse.UpdateResultCode
    StageMissing: UpdateResponse.UpdateResultCode
    StageIntegrityError: UpdateResponse.UpdateResultCode
    ManifestPointerError: UpdateResponse.UpdateResultCode
    TargetMismatch: UpdateResponse.UpdateResultCode
    OutdatedManifestVersion: UpdateResponse.UpdateResultCode
    IntFull: UpdateResponse.UpdateResultCode
    UnspecifiedError: UpdateResponse.UpdateResultCode
    CODE_FIELD_NUMBER: _ClassVar[int]
    code: UpdateResponse.UpdateResultCode
    def __init__(self, code: _Optional[_Union[UpdateResponse.UpdateResultCode, str]] = ...) -> None: ...

class PowerInfoRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class PowerInfoResponse(_message.Message):
    __slots__ = ("key", "value")
    KEY_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    key: str
    value: str
    def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
