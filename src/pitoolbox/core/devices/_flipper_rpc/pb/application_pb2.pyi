from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class AppState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    APP_CLOSED: _ClassVar[AppState]
    APP_STARTED: _ClassVar[AppState]
APP_CLOSED: AppState
APP_STARTED: AppState

class StartRequest(_message.Message):
    __slots__ = ("name", "args")
    NAME_FIELD_NUMBER: _ClassVar[int]
    ARGS_FIELD_NUMBER: _ClassVar[int]
    name: str
    args: str
    def __init__(self, name: _Optional[str] = ..., args: _Optional[str] = ...) -> None: ...

class LockStatusRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class LockStatusResponse(_message.Message):
    __slots__ = ("locked",)
    LOCKED_FIELD_NUMBER: _ClassVar[int]
    locked: bool
    def __init__(self, locked: _Optional[bool] = ...) -> None: ...

class AppExitRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class AppLoadFileRequest(_message.Message):
    __slots__ = ("path",)
    PATH_FIELD_NUMBER: _ClassVar[int]
    path: str
    def __init__(self, path: _Optional[str] = ...) -> None: ...

class AppButtonPressRequest(_message.Message):
    __slots__ = ("args", "index")
    ARGS_FIELD_NUMBER: _ClassVar[int]
    INDEX_FIELD_NUMBER: _ClassVar[int]
    args: str
    index: int
    def __init__(self, args: _Optional[str] = ..., index: _Optional[int] = ...) -> None: ...

class AppButtonReleaseRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class AppButtonPressReleaseRequest(_message.Message):
    __slots__ = ("args", "index")
    ARGS_FIELD_NUMBER: _ClassVar[int]
    INDEX_FIELD_NUMBER: _ClassVar[int]
    args: str
    index: int
    def __init__(self, args: _Optional[str] = ..., index: _Optional[int] = ...) -> None: ...

class AppStateResponse(_message.Message):
    __slots__ = ("state",)
    STATE_FIELD_NUMBER: _ClassVar[int]
    state: AppState
    def __init__(self, state: _Optional[_Union[AppState, str]] = ...) -> None: ...

class GetErrorRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class GetErrorResponse(_message.Message):
    __slots__ = ("code", "text")
    CODE_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    code: int
    text: str
    def __init__(self, code: _Optional[int] = ..., text: _Optional[str] = ...) -> None: ...

class DataExchangeRequest(_message.Message):
    __slots__ = ("data",)
    DATA_FIELD_NUMBER: _ClassVar[int]
    data: bytes
    def __init__(self, data: _Optional[bytes] = ...) -> None: ...
