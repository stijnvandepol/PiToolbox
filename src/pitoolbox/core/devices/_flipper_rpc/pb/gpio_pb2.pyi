from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class GpioPin(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PC0: _ClassVar[GpioPin]
    PC1: _ClassVar[GpioPin]
    PC3: _ClassVar[GpioPin]
    PB2: _ClassVar[GpioPin]
    PB3: _ClassVar[GpioPin]
    PA4: _ClassVar[GpioPin]
    PA6: _ClassVar[GpioPin]
    PA7: _ClassVar[GpioPin]

class GpioPinMode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    OUTPUT: _ClassVar[GpioPinMode]
    INPUT: _ClassVar[GpioPinMode]

class GpioInputPull(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    NO: _ClassVar[GpioInputPull]
    UP: _ClassVar[GpioInputPull]
    DOWN: _ClassVar[GpioInputPull]

class GpioOtgMode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    OFF: _ClassVar[GpioOtgMode]
    ON: _ClassVar[GpioOtgMode]
PC0: GpioPin
PC1: GpioPin
PC3: GpioPin
PB2: GpioPin
PB3: GpioPin
PA4: GpioPin
PA6: GpioPin
PA7: GpioPin
OUTPUT: GpioPinMode
INPUT: GpioPinMode
NO: GpioInputPull
UP: GpioInputPull
DOWN: GpioInputPull
OFF: GpioOtgMode
ON: GpioOtgMode

class SetPinMode(_message.Message):
    __slots__ = ("pin", "mode")
    PIN_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    pin: GpioPin
    mode: GpioPinMode
    def __init__(self, pin: _Optional[_Union[GpioPin, str]] = ..., mode: _Optional[_Union[GpioPinMode, str]] = ...) -> None: ...

class SetInputPull(_message.Message):
    __slots__ = ("pin", "pull_mode")
    PIN_FIELD_NUMBER: _ClassVar[int]
    PULL_MODE_FIELD_NUMBER: _ClassVar[int]
    pin: GpioPin
    pull_mode: GpioInputPull
    def __init__(self, pin: _Optional[_Union[GpioPin, str]] = ..., pull_mode: _Optional[_Union[GpioInputPull, str]] = ...) -> None: ...

class GetPinMode(_message.Message):
    __slots__ = ("pin",)
    PIN_FIELD_NUMBER: _ClassVar[int]
    pin: GpioPin
    def __init__(self, pin: _Optional[_Union[GpioPin, str]] = ...) -> None: ...

class GetPinModeResponse(_message.Message):
    __slots__ = ("mode",)
    MODE_FIELD_NUMBER: _ClassVar[int]
    mode: GpioPinMode
    def __init__(self, mode: _Optional[_Union[GpioPinMode, str]] = ...) -> None: ...

class ReadPin(_message.Message):
    __slots__ = ("pin",)
    PIN_FIELD_NUMBER: _ClassVar[int]
    pin: GpioPin
    def __init__(self, pin: _Optional[_Union[GpioPin, str]] = ...) -> None: ...

class ReadPinResponse(_message.Message):
    __slots__ = ("value",)
    VALUE_FIELD_NUMBER: _ClassVar[int]
    value: int
    def __init__(self, value: _Optional[int] = ...) -> None: ...

class WritePin(_message.Message):
    __slots__ = ("pin", "value")
    PIN_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    pin: GpioPin
    value: int
    def __init__(self, pin: _Optional[_Union[GpioPin, str]] = ..., value: _Optional[int] = ...) -> None: ...

class GetOtgMode(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class GetOtgModeResponse(_message.Message):
    __slots__ = ("mode",)
    MODE_FIELD_NUMBER: _ClassVar[int]
    mode: GpioOtgMode
    def __init__(self, mode: _Optional[_Union[GpioOtgMode, str]] = ...) -> None: ...

class SetOtgMode(_message.Message):
    __slots__ = ("mode",)
    MODE_FIELD_NUMBER: _ClassVar[int]
    mode: GpioOtgMode
    def __init__(self, mode: _Optional[_Union[GpioOtgMode, str]] = ...) -> None: ...
