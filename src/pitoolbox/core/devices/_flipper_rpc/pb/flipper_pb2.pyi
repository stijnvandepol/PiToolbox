from . import storage_pb2 as _storage_pb2
from . import system_pb2 as _system_pb2
from . import application_pb2 as _application_pb2
from . import gui_pb2 as _gui_pb2
from . import gpio_pb2 as _gpio_pb2
from . import property_pb2 as _property_pb2
from . import desktop_pb2 as _desktop_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class CommandStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    OK: _ClassVar[CommandStatus]
    ERROR: _ClassVar[CommandStatus]
    ERROR_DECODE: _ClassVar[CommandStatus]
    ERROR_NOT_IMPLEMENTED: _ClassVar[CommandStatus]
    ERROR_BUSY: _ClassVar[CommandStatus]
    ERROR_CONTINUOUS_COMMAND_INTERRUPTED: _ClassVar[CommandStatus]
    ERROR_INVALID_PARAMETERS: _ClassVar[CommandStatus]
    ERROR_STORAGE_NOT_READY: _ClassVar[CommandStatus]
    ERROR_STORAGE_EXIST: _ClassVar[CommandStatus]
    ERROR_STORAGE_NOT_EXIST: _ClassVar[CommandStatus]
    ERROR_STORAGE_INVALID_PARAMETER: _ClassVar[CommandStatus]
    ERROR_STORAGE_DENIED: _ClassVar[CommandStatus]
    ERROR_STORAGE_INVALID_NAME: _ClassVar[CommandStatus]
    ERROR_STORAGE_INTERNAL: _ClassVar[CommandStatus]
    ERROR_STORAGE_NOT_IMPLEMENTED: _ClassVar[CommandStatus]
    ERROR_STORAGE_ALREADY_OPEN: _ClassVar[CommandStatus]
    ERROR_STORAGE_DIR_NOT_EMPTY: _ClassVar[CommandStatus]
    ERROR_APP_CANT_START: _ClassVar[CommandStatus]
    ERROR_APP_SYSTEM_LOCKED: _ClassVar[CommandStatus]
    ERROR_APP_NOT_RUNNING: _ClassVar[CommandStatus]
    ERROR_APP_CMD_ERROR: _ClassVar[CommandStatus]
    ERROR_VIRTUAL_DISPLAY_ALREADY_STARTED: _ClassVar[CommandStatus]
    ERROR_VIRTUAL_DISPLAY_NOT_STARTED: _ClassVar[CommandStatus]
    ERROR_GPIO_MODE_INCORRECT: _ClassVar[CommandStatus]
    ERROR_GPIO_UNKNOWN_PIN_MODE: _ClassVar[CommandStatus]
OK: CommandStatus
ERROR: CommandStatus
ERROR_DECODE: CommandStatus
ERROR_NOT_IMPLEMENTED: CommandStatus
ERROR_BUSY: CommandStatus
ERROR_CONTINUOUS_COMMAND_INTERRUPTED: CommandStatus
ERROR_INVALID_PARAMETERS: CommandStatus
ERROR_STORAGE_NOT_READY: CommandStatus
ERROR_STORAGE_EXIST: CommandStatus
ERROR_STORAGE_NOT_EXIST: CommandStatus
ERROR_STORAGE_INVALID_PARAMETER: CommandStatus
ERROR_STORAGE_DENIED: CommandStatus
ERROR_STORAGE_INVALID_NAME: CommandStatus
ERROR_STORAGE_INTERNAL: CommandStatus
ERROR_STORAGE_NOT_IMPLEMENTED: CommandStatus
ERROR_STORAGE_ALREADY_OPEN: CommandStatus
ERROR_STORAGE_DIR_NOT_EMPTY: CommandStatus
ERROR_APP_CANT_START: CommandStatus
ERROR_APP_SYSTEM_LOCKED: CommandStatus
ERROR_APP_NOT_RUNNING: CommandStatus
ERROR_APP_CMD_ERROR: CommandStatus
ERROR_VIRTUAL_DISPLAY_ALREADY_STARTED: CommandStatus
ERROR_VIRTUAL_DISPLAY_NOT_STARTED: CommandStatus
ERROR_GPIO_MODE_INCORRECT: CommandStatus
ERROR_GPIO_UNKNOWN_PIN_MODE: CommandStatus

class Empty(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class StopSession(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class Main(_message.Message):
    __slots__ = ("command_id", "command_status", "has_next", "empty", "stop_session", "system_ping_request", "system_ping_response", "system_reboot_request", "system_device_info_request", "system_device_info_response", "system_factory_reset_request", "system_get_datetime_request", "system_get_datetime_response", "system_set_datetime_request", "system_play_audiovisual_alert_request", "system_protobuf_version_request", "system_protobuf_version_response", "system_update_request", "system_update_response", "system_power_info_request", "system_power_info_response", "storage_info_request", "storage_info_response", "storage_timestamp_request", "storage_timestamp_response", "storage_stat_request", "storage_stat_response", "storage_list_request", "storage_list_response", "storage_read_request", "storage_read_response", "storage_write_request", "storage_delete_request", "storage_mkdir_request", "storage_md5sum_request", "storage_md5sum_response", "storage_rename_request", "storage_backup_create_request", "storage_backup_restore_request", "storage_tar_extract_request", "app_start_request", "app_lock_status_request", "app_lock_status_response", "app_exit_request", "app_load_file_request", "app_button_press_request", "app_button_release_request", "app_button_press_release_request", "app_get_error_request", "app_get_error_response", "app_data_exchange_request", "gui_start_screen_stream_request", "gui_stop_screen_stream_request", "gui_screen_frame", "gui_send_input_event_request", "gui_start_virtual_display_request", "gui_stop_virtual_display_request", "gpio_set_pin_mode", "gpio_set_input_pull", "gpio_get_pin_mode", "gpio_get_pin_mode_response", "gpio_read_pin", "gpio_read_pin_response", "gpio_write_pin", "gpio_get_otg_mode", "gpio_get_otg_mode_response", "gpio_set_otg_mode", "app_state_response", "property_get_request", "property_get_response", "desktop_is_locked_request", "desktop_unlock_request", "desktop_status_subscribe_request", "desktop_status_unsubscribe_request", "desktop_status")
    COMMAND_ID_FIELD_NUMBER: _ClassVar[int]
    COMMAND_STATUS_FIELD_NUMBER: _ClassVar[int]
    HAS_NEXT_FIELD_NUMBER: _ClassVar[int]
    EMPTY_FIELD_NUMBER: _ClassVar[int]
    STOP_SESSION_FIELD_NUMBER: _ClassVar[int]
    SYSTEM_PING_REQUEST_FIELD_NUMBER: _ClassVar[int]
    SYSTEM_PING_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    SYSTEM_REBOOT_REQUEST_FIELD_NUMBER: _ClassVar[int]
    SYSTEM_DEVICE_INFO_REQUEST_FIELD_NUMBER: _ClassVar[int]
    SYSTEM_DEVICE_INFO_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    SYSTEM_FACTORY_RESET_REQUEST_FIELD_NUMBER: _ClassVar[int]
    SYSTEM_GET_DATETIME_REQUEST_FIELD_NUMBER: _ClassVar[int]
    SYSTEM_GET_DATETIME_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    SYSTEM_SET_DATETIME_REQUEST_FIELD_NUMBER: _ClassVar[int]
    SYSTEM_PLAY_AUDIOVISUAL_ALERT_REQUEST_FIELD_NUMBER: _ClassVar[int]
    SYSTEM_PROTOBUF_VERSION_REQUEST_FIELD_NUMBER: _ClassVar[int]
    SYSTEM_PROTOBUF_VERSION_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    SYSTEM_UPDATE_REQUEST_FIELD_NUMBER: _ClassVar[int]
    SYSTEM_UPDATE_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    SYSTEM_POWER_INFO_REQUEST_FIELD_NUMBER: _ClassVar[int]
    SYSTEM_POWER_INFO_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    STORAGE_INFO_REQUEST_FIELD_NUMBER: _ClassVar[int]
    STORAGE_INFO_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    STORAGE_TIMESTAMP_REQUEST_FIELD_NUMBER: _ClassVar[int]
    STORAGE_TIMESTAMP_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    STORAGE_STAT_REQUEST_FIELD_NUMBER: _ClassVar[int]
    STORAGE_STAT_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    STORAGE_LIST_REQUEST_FIELD_NUMBER: _ClassVar[int]
    STORAGE_LIST_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    STORAGE_READ_REQUEST_FIELD_NUMBER: _ClassVar[int]
    STORAGE_READ_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    STORAGE_WRITE_REQUEST_FIELD_NUMBER: _ClassVar[int]
    STORAGE_DELETE_REQUEST_FIELD_NUMBER: _ClassVar[int]
    STORAGE_MKDIR_REQUEST_FIELD_NUMBER: _ClassVar[int]
    STORAGE_MD5SUM_REQUEST_FIELD_NUMBER: _ClassVar[int]
    STORAGE_MD5SUM_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    STORAGE_RENAME_REQUEST_FIELD_NUMBER: _ClassVar[int]
    STORAGE_BACKUP_CREATE_REQUEST_FIELD_NUMBER: _ClassVar[int]
    STORAGE_BACKUP_RESTORE_REQUEST_FIELD_NUMBER: _ClassVar[int]
    STORAGE_TAR_EXTRACT_REQUEST_FIELD_NUMBER: _ClassVar[int]
    APP_START_REQUEST_FIELD_NUMBER: _ClassVar[int]
    APP_LOCK_STATUS_REQUEST_FIELD_NUMBER: _ClassVar[int]
    APP_LOCK_STATUS_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    APP_EXIT_REQUEST_FIELD_NUMBER: _ClassVar[int]
    APP_LOAD_FILE_REQUEST_FIELD_NUMBER: _ClassVar[int]
    APP_BUTTON_PRESS_REQUEST_FIELD_NUMBER: _ClassVar[int]
    APP_BUTTON_RELEASE_REQUEST_FIELD_NUMBER: _ClassVar[int]
    APP_BUTTON_PRESS_RELEASE_REQUEST_FIELD_NUMBER: _ClassVar[int]
    APP_GET_ERROR_REQUEST_FIELD_NUMBER: _ClassVar[int]
    APP_GET_ERROR_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    APP_DATA_EXCHANGE_REQUEST_FIELD_NUMBER: _ClassVar[int]
    GUI_START_SCREEN_STREAM_REQUEST_FIELD_NUMBER: _ClassVar[int]
    GUI_STOP_SCREEN_STREAM_REQUEST_FIELD_NUMBER: _ClassVar[int]
    GUI_SCREEN_FRAME_FIELD_NUMBER: _ClassVar[int]
    GUI_SEND_INPUT_EVENT_REQUEST_FIELD_NUMBER: _ClassVar[int]
    GUI_START_VIRTUAL_DISPLAY_REQUEST_FIELD_NUMBER: _ClassVar[int]
    GUI_STOP_VIRTUAL_DISPLAY_REQUEST_FIELD_NUMBER: _ClassVar[int]
    GPIO_SET_PIN_MODE_FIELD_NUMBER: _ClassVar[int]
    GPIO_SET_INPUT_PULL_FIELD_NUMBER: _ClassVar[int]
    GPIO_GET_PIN_MODE_FIELD_NUMBER: _ClassVar[int]
    GPIO_GET_PIN_MODE_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    GPIO_READ_PIN_FIELD_NUMBER: _ClassVar[int]
    GPIO_READ_PIN_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    GPIO_WRITE_PIN_FIELD_NUMBER: _ClassVar[int]
    GPIO_GET_OTG_MODE_FIELD_NUMBER: _ClassVar[int]
    GPIO_GET_OTG_MODE_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    GPIO_SET_OTG_MODE_FIELD_NUMBER: _ClassVar[int]
    APP_STATE_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    PROPERTY_GET_REQUEST_FIELD_NUMBER: _ClassVar[int]
    PROPERTY_GET_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    DESKTOP_IS_LOCKED_REQUEST_FIELD_NUMBER: _ClassVar[int]
    DESKTOP_UNLOCK_REQUEST_FIELD_NUMBER: _ClassVar[int]
    DESKTOP_STATUS_SUBSCRIBE_REQUEST_FIELD_NUMBER: _ClassVar[int]
    DESKTOP_STATUS_UNSUBSCRIBE_REQUEST_FIELD_NUMBER: _ClassVar[int]
    DESKTOP_STATUS_FIELD_NUMBER: _ClassVar[int]
    command_id: int
    command_status: CommandStatus
    has_next: bool
    empty: Empty
    stop_session: StopSession
    system_ping_request: _system_pb2.PingRequest
    system_ping_response: _system_pb2.PingResponse
    system_reboot_request: _system_pb2.RebootRequest
    system_device_info_request: _system_pb2.DeviceInfoRequest
    system_device_info_response: _system_pb2.DeviceInfoResponse
    system_factory_reset_request: _system_pb2.FactoryResetRequest
    system_get_datetime_request: _system_pb2.GetDateTimeRequest
    system_get_datetime_response: _system_pb2.GetDateTimeResponse
    system_set_datetime_request: _system_pb2.SetDateTimeRequest
    system_play_audiovisual_alert_request: _system_pb2.PlayAudiovisualAlertRequest
    system_protobuf_version_request: _system_pb2.ProtobufVersionRequest
    system_protobuf_version_response: _system_pb2.ProtobufVersionResponse
    system_update_request: _system_pb2.UpdateRequest
    system_update_response: _system_pb2.UpdateResponse
    system_power_info_request: _system_pb2.PowerInfoRequest
    system_power_info_response: _system_pb2.PowerInfoResponse
    storage_info_request: _storage_pb2.InfoRequest
    storage_info_response: _storage_pb2.InfoResponse
    storage_timestamp_request: _storage_pb2.TimestampRequest
    storage_timestamp_response: _storage_pb2.TimestampResponse
    storage_stat_request: _storage_pb2.StatRequest
    storage_stat_response: _storage_pb2.StatResponse
    storage_list_request: _storage_pb2.ListRequest
    storage_list_response: _storage_pb2.ListResponse
    storage_read_request: _storage_pb2.ReadRequest
    storage_read_response: _storage_pb2.ReadResponse
    storage_write_request: _storage_pb2.WriteRequest
    storage_delete_request: _storage_pb2.DeleteRequest
    storage_mkdir_request: _storage_pb2.MkdirRequest
    storage_md5sum_request: _storage_pb2.Md5sumRequest
    storage_md5sum_response: _storage_pb2.Md5sumResponse
    storage_rename_request: _storage_pb2.RenameRequest
    storage_backup_create_request: _storage_pb2.BackupCreateRequest
    storage_backup_restore_request: _storage_pb2.BackupRestoreRequest
    storage_tar_extract_request: _storage_pb2.TarExtractRequest
    app_start_request: _application_pb2.StartRequest
    app_lock_status_request: _application_pb2.LockStatusRequest
    app_lock_status_response: _application_pb2.LockStatusResponse
    app_exit_request: _application_pb2.AppExitRequest
    app_load_file_request: _application_pb2.AppLoadFileRequest
    app_button_press_request: _application_pb2.AppButtonPressRequest
    app_button_release_request: _application_pb2.AppButtonReleaseRequest
    app_button_press_release_request: _application_pb2.AppButtonPressReleaseRequest
    app_get_error_request: _application_pb2.GetErrorRequest
    app_get_error_response: _application_pb2.GetErrorResponse
    app_data_exchange_request: _application_pb2.DataExchangeRequest
    gui_start_screen_stream_request: _gui_pb2.StartScreenStreamRequest
    gui_stop_screen_stream_request: _gui_pb2.StopScreenStreamRequest
    gui_screen_frame: _gui_pb2.ScreenFrame
    gui_send_input_event_request: _gui_pb2.SendInputEventRequest
    gui_start_virtual_display_request: _gui_pb2.StartVirtualDisplayRequest
    gui_stop_virtual_display_request: _gui_pb2.StopVirtualDisplayRequest
    gpio_set_pin_mode: _gpio_pb2.SetPinMode
    gpio_set_input_pull: _gpio_pb2.SetInputPull
    gpio_get_pin_mode: _gpio_pb2.GetPinMode
    gpio_get_pin_mode_response: _gpio_pb2.GetPinModeResponse
    gpio_read_pin: _gpio_pb2.ReadPin
    gpio_read_pin_response: _gpio_pb2.ReadPinResponse
    gpio_write_pin: _gpio_pb2.WritePin
    gpio_get_otg_mode: _gpio_pb2.GetOtgMode
    gpio_get_otg_mode_response: _gpio_pb2.GetOtgModeResponse
    gpio_set_otg_mode: _gpio_pb2.SetOtgMode
    app_state_response: _application_pb2.AppStateResponse
    property_get_request: _property_pb2.GetRequest
    property_get_response: _property_pb2.GetResponse
    desktop_is_locked_request: _desktop_pb2.IsLockedRequest
    desktop_unlock_request: _desktop_pb2.UnlockRequest
    desktop_status_subscribe_request: _desktop_pb2.StatusSubscribeRequest
    desktop_status_unsubscribe_request: _desktop_pb2.StatusUnsubscribeRequest
    desktop_status: _desktop_pb2.Status
    def __init__(self, command_id: _Optional[int] = ..., command_status: _Optional[_Union[CommandStatus, str]] = ..., has_next: _Optional[bool] = ..., empty: _Optional[_Union[Empty, _Mapping]] = ..., stop_session: _Optional[_Union[StopSession, _Mapping]] = ..., system_ping_request: _Optional[_Union[_system_pb2.PingRequest, _Mapping]] = ..., system_ping_response: _Optional[_Union[_system_pb2.PingResponse, _Mapping]] = ..., system_reboot_request: _Optional[_Union[_system_pb2.RebootRequest, _Mapping]] = ..., system_device_info_request: _Optional[_Union[_system_pb2.DeviceInfoRequest, _Mapping]] = ..., system_device_info_response: _Optional[_Union[_system_pb2.DeviceInfoResponse, _Mapping]] = ..., system_factory_reset_request: _Optional[_Union[_system_pb2.FactoryResetRequest, _Mapping]] = ..., system_get_datetime_request: _Optional[_Union[_system_pb2.GetDateTimeRequest, _Mapping]] = ..., system_get_datetime_response: _Optional[_Union[_system_pb2.GetDateTimeResponse, _Mapping]] = ..., system_set_datetime_request: _Optional[_Union[_system_pb2.SetDateTimeRequest, _Mapping]] = ..., system_play_audiovisual_alert_request: _Optional[_Union[_system_pb2.PlayAudiovisualAlertRequest, _Mapping]] = ..., system_protobuf_version_request: _Optional[_Union[_system_pb2.ProtobufVersionRequest, _Mapping]] = ..., system_protobuf_version_response: _Optional[_Union[_system_pb2.ProtobufVersionResponse, _Mapping]] = ..., system_update_request: _Optional[_Union[_system_pb2.UpdateRequest, _Mapping]] = ..., system_update_response: _Optional[_Union[_system_pb2.UpdateResponse, _Mapping]] = ..., system_power_info_request: _Optional[_Union[_system_pb2.PowerInfoRequest, _Mapping]] = ..., system_power_info_response: _Optional[_Union[_system_pb2.PowerInfoResponse, _Mapping]] = ..., storage_info_request: _Optional[_Union[_storage_pb2.InfoRequest, _Mapping]] = ..., storage_info_response: _Optional[_Union[_storage_pb2.InfoResponse, _Mapping]] = ..., storage_timestamp_request: _Optional[_Union[_storage_pb2.TimestampRequest, _Mapping]] = ..., storage_timestamp_response: _Optional[_Union[_storage_pb2.TimestampResponse, _Mapping]] = ..., storage_stat_request: _Optional[_Union[_storage_pb2.StatRequest, _Mapping]] = ..., storage_stat_response: _Optional[_Union[_storage_pb2.StatResponse, _Mapping]] = ..., storage_list_request: _Optional[_Union[_storage_pb2.ListRequest, _Mapping]] = ..., storage_list_response: _Optional[_Union[_storage_pb2.ListResponse, _Mapping]] = ..., storage_read_request: _Optional[_Union[_storage_pb2.ReadRequest, _Mapping]] = ..., storage_read_response: _Optional[_Union[_storage_pb2.ReadResponse, _Mapping]] = ..., storage_write_request: _Optional[_Union[_storage_pb2.WriteRequest, _Mapping]] = ..., storage_delete_request: _Optional[_Union[_storage_pb2.DeleteRequest, _Mapping]] = ..., storage_mkdir_request: _Optional[_Union[_storage_pb2.MkdirRequest, _Mapping]] = ..., storage_md5sum_request: _Optional[_Union[_storage_pb2.Md5sumRequest, _Mapping]] = ..., storage_md5sum_response: _Optional[_Union[_storage_pb2.Md5sumResponse, _Mapping]] = ..., storage_rename_request: _Optional[_Union[_storage_pb2.RenameRequest, _Mapping]] = ..., storage_backup_create_request: _Optional[_Union[_storage_pb2.BackupCreateRequest, _Mapping]] = ..., storage_backup_restore_request: _Optional[_Union[_storage_pb2.BackupRestoreRequest, _Mapping]] = ..., storage_tar_extract_request: _Optional[_Union[_storage_pb2.TarExtractRequest, _Mapping]] = ..., app_start_request: _Optional[_Union[_application_pb2.StartRequest, _Mapping]] = ..., app_lock_status_request: _Optional[_Union[_application_pb2.LockStatusRequest, _Mapping]] = ..., app_lock_status_response: _Optional[_Union[_application_pb2.LockStatusResponse, _Mapping]] = ..., app_exit_request: _Optional[_Union[_application_pb2.AppExitRequest, _Mapping]] = ..., app_load_file_request: _Optional[_Union[_application_pb2.AppLoadFileRequest, _Mapping]] = ..., app_button_press_request: _Optional[_Union[_application_pb2.AppButtonPressRequest, _Mapping]] = ..., app_button_release_request: _Optional[_Union[_application_pb2.AppButtonReleaseRequest, _Mapping]] = ..., app_button_press_release_request: _Optional[_Union[_application_pb2.AppButtonPressReleaseRequest, _Mapping]] = ..., app_get_error_request: _Optional[_Union[_application_pb2.GetErrorRequest, _Mapping]] = ..., app_get_error_response: _Optional[_Union[_application_pb2.GetErrorResponse, _Mapping]] = ..., app_data_exchange_request: _Optional[_Union[_application_pb2.DataExchangeRequest, _Mapping]] = ..., gui_start_screen_stream_request: _Optional[_Union[_gui_pb2.StartScreenStreamRequest, _Mapping]] = ..., gui_stop_screen_stream_request: _Optional[_Union[_gui_pb2.StopScreenStreamRequest, _Mapping]] = ..., gui_screen_frame: _Optional[_Union[_gui_pb2.ScreenFrame, _Mapping]] = ..., gui_send_input_event_request: _Optional[_Union[_gui_pb2.SendInputEventRequest, _Mapping]] = ..., gui_start_virtual_display_request: _Optional[_Union[_gui_pb2.StartVirtualDisplayRequest, _Mapping]] = ..., gui_stop_virtual_display_request: _Optional[_Union[_gui_pb2.StopVirtualDisplayRequest, _Mapping]] = ..., gpio_set_pin_mode: _Optional[_Union[_gpio_pb2.SetPinMode, _Mapping]] = ..., gpio_set_input_pull: _Optional[_Union[_gpio_pb2.SetInputPull, _Mapping]] = ..., gpio_get_pin_mode: _Optional[_Union[_gpio_pb2.GetPinMode, _Mapping]] = ..., gpio_get_pin_mode_response: _Optional[_Union[_gpio_pb2.GetPinModeResponse, _Mapping]] = ..., gpio_read_pin: _Optional[_Union[_gpio_pb2.ReadPin, _Mapping]] = ..., gpio_read_pin_response: _Optional[_Union[_gpio_pb2.ReadPinResponse, _Mapping]] = ..., gpio_write_pin: _Optional[_Union[_gpio_pb2.WritePin, _Mapping]] = ..., gpio_get_otg_mode: _Optional[_Union[_gpio_pb2.GetOtgMode, _Mapping]] = ..., gpio_get_otg_mode_response: _Optional[_Union[_gpio_pb2.GetOtgModeResponse, _Mapping]] = ..., gpio_set_otg_mode: _Optional[_Union[_gpio_pb2.SetOtgMode, _Mapping]] = ..., app_state_response: _Optional[_Union[_application_pb2.AppStateResponse, _Mapping]] = ..., property_get_request: _Optional[_Union[_property_pb2.GetRequest, _Mapping]] = ..., property_get_response: _Optional[_Union[_property_pb2.GetResponse, _Mapping]] = ..., desktop_is_locked_request: _Optional[_Union[_desktop_pb2.IsLockedRequest, _Mapping]] = ..., desktop_unlock_request: _Optional[_Union[_desktop_pb2.UnlockRequest, _Mapping]] = ..., desktop_status_subscribe_request: _Optional[_Union[_desktop_pb2.StatusSubscribeRequest, _Mapping]] = ..., desktop_status_unsubscribe_request: _Optional[_Union[_desktop_pb2.StatusUnsubscribeRequest, _Mapping]] = ..., desktop_status: _Optional[_Union[_desktop_pb2.Status, _Mapping]] = ...) -> None: ...

class Region(_message.Message):
    __slots__ = ("country_code", "bands")
    class Band(_message.Message):
        __slots__ = ("start", "end", "power_limit", "duty_cycle")
        START_FIELD_NUMBER: _ClassVar[int]
        END_FIELD_NUMBER: _ClassVar[int]
        POWER_LIMIT_FIELD_NUMBER: _ClassVar[int]
        DUTY_CYCLE_FIELD_NUMBER: _ClassVar[int]
        start: int
        end: int
        power_limit: int
        duty_cycle: int
        def __init__(self, start: _Optional[int] = ..., end: _Optional[int] = ..., power_limit: _Optional[int] = ..., duty_cycle: _Optional[int] = ...) -> None: ...
    COUNTRY_CODE_FIELD_NUMBER: _ClassVar[int]
    BANDS_FIELD_NUMBER: _ClassVar[int]
    country_code: bytes
    bands: _containers.RepeatedCompositeFieldContainer[Region.Band]
    def __init__(self, country_code: _Optional[bytes] = ..., bands: _Optional[_Iterable[_Union[Region.Band, _Mapping]]] = ...) -> None: ...
