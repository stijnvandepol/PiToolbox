from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor

class IsLockedRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class UnlockRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class StatusSubscribeRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class StatusUnsubscribeRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class Status(_message.Message):
    __slots__ = ("locked",)
    LOCKED_FIELD_NUMBER: _ClassVar[int]
    locked: bool
    def __init__(self, locked: _Optional[bool] = ...) -> None: ...
