from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class File(_message.Message):
    __slots__ = ("type", "name", "size", "data", "md5sum")
    class FileType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        FILE: _ClassVar[File.FileType]
        DIR: _ClassVar[File.FileType]
    FILE: File.FileType
    DIR: File.FileType
    TYPE_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    MD5SUM_FIELD_NUMBER: _ClassVar[int]
    type: File.FileType
    name: str
    size: int
    data: bytes
    md5sum: str
    def __init__(self, type: _Optional[_Union[File.FileType, str]] = ..., name: _Optional[str] = ..., size: _Optional[int] = ..., data: _Optional[bytes] = ..., md5sum: _Optional[str] = ...) -> None: ...

class InfoRequest(_message.Message):
    __slots__ = ("path",)
    PATH_FIELD_NUMBER: _ClassVar[int]
    path: str
    def __init__(self, path: _Optional[str] = ...) -> None: ...

class InfoResponse(_message.Message):
    __slots__ = ("total_space", "free_space")
    TOTAL_SPACE_FIELD_NUMBER: _ClassVar[int]
    FREE_SPACE_FIELD_NUMBER: _ClassVar[int]
    total_space: int
    free_space: int
    def __init__(self, total_space: _Optional[int] = ..., free_space: _Optional[int] = ...) -> None: ...

class TimestampRequest(_message.Message):
    __slots__ = ("path",)
    PATH_FIELD_NUMBER: _ClassVar[int]
    path: str
    def __init__(self, path: _Optional[str] = ...) -> None: ...

class TimestampResponse(_message.Message):
    __slots__ = ("timestamp",)
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    timestamp: int
    def __init__(self, timestamp: _Optional[int] = ...) -> None: ...

class StatRequest(_message.Message):
    __slots__ = ("path",)
    PATH_FIELD_NUMBER: _ClassVar[int]
    path: str
    def __init__(self, path: _Optional[str] = ...) -> None: ...

class StatResponse(_message.Message):
    __slots__ = ("file",)
    FILE_FIELD_NUMBER: _ClassVar[int]
    file: File
    def __init__(self, file: _Optional[_Union[File, _Mapping]] = ...) -> None: ...

class ListRequest(_message.Message):
    __slots__ = ("path", "include_md5", "filter_max_size")
    PATH_FIELD_NUMBER: _ClassVar[int]
    INCLUDE_MD5_FIELD_NUMBER: _ClassVar[int]
    FILTER_MAX_SIZE_FIELD_NUMBER: _ClassVar[int]
    path: str
    include_md5: bool
    filter_max_size: int
    def __init__(self, path: _Optional[str] = ..., include_md5: _Optional[bool] = ..., filter_max_size: _Optional[int] = ...) -> None: ...

class ListResponse(_message.Message):
    __slots__ = ("file",)
    FILE_FIELD_NUMBER: _ClassVar[int]
    file: _containers.RepeatedCompositeFieldContainer[File]
    def __init__(self, file: _Optional[_Iterable[_Union[File, _Mapping]]] = ...) -> None: ...

class ReadRequest(_message.Message):
    __slots__ = ("path",)
    PATH_FIELD_NUMBER: _ClassVar[int]
    path: str
    def __init__(self, path: _Optional[str] = ...) -> None: ...

class ReadResponse(_message.Message):
    __slots__ = ("file",)
    FILE_FIELD_NUMBER: _ClassVar[int]
    file: File
    def __init__(self, file: _Optional[_Union[File, _Mapping]] = ...) -> None: ...

class WriteRequest(_message.Message):
    __slots__ = ("path", "file")
    PATH_FIELD_NUMBER: _ClassVar[int]
    FILE_FIELD_NUMBER: _ClassVar[int]
    path: str
    file: File
    def __init__(self, path: _Optional[str] = ..., file: _Optional[_Union[File, _Mapping]] = ...) -> None: ...

class DeleteRequest(_message.Message):
    __slots__ = ("path", "recursive")
    PATH_FIELD_NUMBER: _ClassVar[int]
    RECURSIVE_FIELD_NUMBER: _ClassVar[int]
    path: str
    recursive: bool
    def __init__(self, path: _Optional[str] = ..., recursive: _Optional[bool] = ...) -> None: ...

class MkdirRequest(_message.Message):
    __slots__ = ("path",)
    PATH_FIELD_NUMBER: _ClassVar[int]
    path: str
    def __init__(self, path: _Optional[str] = ...) -> None: ...

class Md5sumRequest(_message.Message):
    __slots__ = ("path",)
    PATH_FIELD_NUMBER: _ClassVar[int]
    path: str
    def __init__(self, path: _Optional[str] = ...) -> None: ...

class Md5sumResponse(_message.Message):
    __slots__ = ("md5sum",)
    MD5SUM_FIELD_NUMBER: _ClassVar[int]
    md5sum: str
    def __init__(self, md5sum: _Optional[str] = ...) -> None: ...

class RenameRequest(_message.Message):
    __slots__ = ("old_path", "new_path")
    OLD_PATH_FIELD_NUMBER: _ClassVar[int]
    NEW_PATH_FIELD_NUMBER: _ClassVar[int]
    old_path: str
    new_path: str
    def __init__(self, old_path: _Optional[str] = ..., new_path: _Optional[str] = ...) -> None: ...

class BackupCreateRequest(_message.Message):
    __slots__ = ("archive_path",)
    ARCHIVE_PATH_FIELD_NUMBER: _ClassVar[int]
    archive_path: str
    def __init__(self, archive_path: _Optional[str] = ...) -> None: ...

class BackupRestoreRequest(_message.Message):
    __slots__ = ("archive_path",)
    ARCHIVE_PATH_FIELD_NUMBER: _ClassVar[int]
    archive_path: str
    def __init__(self, archive_path: _Optional[str] = ...) -> None: ...

class TarExtractRequest(_message.Message):
    __slots__ = ("tar_path", "out_path")
    TAR_PATH_FIELD_NUMBER: _ClassVar[int]
    OUT_PATH_FIELD_NUMBER: _ClassVar[int]
    tar_path: str
    out_path: str
    def __init__(self, tar_path: _Optional[str] = ..., out_path: _Optional[str] = ...) -> None: ...
