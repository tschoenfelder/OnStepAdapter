from abc import ABC, abstractmethod


class FocuserPort(ABC):
    @abstractmethod
    def connect(self) -> bool: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def move(self, steps: int) -> None: ...

    @abstractmethod
    def get_position(self) -> int: ...

    @abstractmethod
    def get_max_position(self) -> int: ...

    @abstractmethod
    def is_moving(self) -> bool: ...

    @abstractmethod
    def stop(self) -> None: ...

    @property
    @abstractmethod
    def is_available(self) -> bool: ...
