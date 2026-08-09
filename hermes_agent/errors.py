"""Bounded runtime errors."""


class AgentError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class AgentCancelled(AgentError):
    def __init__(self, message: str = "Hermes coding runtime was cancelled."):
        super().__init__("HERMES_AGENT_CANCELLED", message)

