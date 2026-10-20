"""Application errors with a stable HTTP/JSON contract."""


class AppError(Exception):
    def __init__(self, message, status=400, code="invalid_request", details=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details or {}


class ControllerError(AppError):
    pass


class ControllerOccupiedError(ControllerError):
    def __init__(self, message="This app is open elsewhere. Take Control to use it here."):
        super().__init__(message, 423, "controller_occupied")


class ControllerRequiredError(ControllerError):
    def __init__(self, message="Take Control to use this app."):
        super().__init__(message, 409, "controller_required")


class ControllerTransferredError(ControllerError):
    def __init__(
        self, message="Control was transferred to another page. Take Control to use it here."
    ):
        super().__init__(message, 423, "controller_transferred")
