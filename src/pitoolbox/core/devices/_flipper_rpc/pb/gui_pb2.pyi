from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class InputKey(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    UP: _ClassVar[InputKey]
    DOWN: _ClassVar[InputKey]
    RIGHT: _ClassVar[InputKey]
    LEFT: _ClassVar[InputKey]
    OK: _ClassVar[InputKey]
    BACK: _ClassVar[InputKey]

class InputType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PRESS: _ClassVar[InputType]
    RELEASE: _ClassVar[InputType]
    SHORT: _ClassVar[InputType]
    LONG: _ClassVar[InputType]
    REPEAT: _ClassVar[InputType]

class ScreenOrientation(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    HORIZONTAL: _ClassVar[ScreenOrientation]
    HORIZONTAL_FLIP: _ClassVar[ScreenOrientation]
    VERTICAL: _ClassVar[ScreenOrientation]
    VERTICAL_FLIP: _ClassVar[ScreenOrientation]
UP: InputKey
DOWN: InputKey
RIGHT: InputKey
LEFT: InputKey
OK: InputKey
BACK: InputKey
PRESS: InputType
RELEASE: InputType
SHORT: InputType
LONG: InputType
REPEAT: InputType
HORIZONTAL: ScreenOrientation
HORIZONTAL_FLIP: ScreenOrientation
VERTICAL: ScreenOrientation
VERTICAL_FLIP: ScreenOrientation

class ScreenFrame(_message.Message):
    __slots__ = ("data", "orientation")
    DATA_FIELD_NUMBER: _ClassVar[int]
    ORIENTATION_FIELD_NUMBER: _ClassVar[int]
    data: bytes
    orientation: ScreenOrientation
    def __init__(self, data: _Optional[bytes] = ..., orientation: _Optional[_Union[ScreenOrientation, str]] = ...) -> None: ...

class StartScreenStreamRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class StopScreenStreamRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class SendInputEventRequest(_message.Message):
    __slots__ = ("key", "type")
    KEY_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    key: InputKey
    type: InputType
    def __init__(self, key: _Optional[_Union[InputKey, str]] = ..., type: _Optional[_Union[InputType, str]] = ...) -> None: ...

class StartVirtualDisplayRequest(_message.Message):
    __slots__ = ("first_frame", "send_input")
    FIRST_FRAME_FIELD_NUMBER: _ClassVar[int]
    SEND_INPUT_FIELD_NUMBER: _ClassVar[int]
    first_frame: ScreenFrame
    send_input: bool
    def __init__(self, first_frame: _Optional[_Union[ScreenFrame, _Mapping]] = ..., send_input: _Optional[bool] = ...) -> None: ...

class StopVirtualDisplayRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...
