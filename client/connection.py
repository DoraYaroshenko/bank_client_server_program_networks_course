"""Network layer of the bank client.

Public methods return built-in Python values (str, int, list, tuple, None), except
history types which use the ``TxType`` enum so the GUI does not deal with raw
type strings. On a rejected or unexpected server reply they raise ``RequestFailed``
with a user-facing message. Transport failures raise ``ConnectionError`` / ``OSError``.
"""
from __future__ import annotations

import socket
import struct
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from typing import Callable, List, Optional, Tuple, Union


class TxType(Enum):
    """Finite set of history entry types the GUI understands.

    ``req_history`` returns these instead of free-form strings. The GUI turns
    them into display text; wire values from the server are mapped here.
    """

    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"
    TRANSFER_IN = "TRANSFER_IN"
    TRANSFER_OUT = "TRANSFER_OUT"
    CHARGE = "CHARGE"
    LOAN = "LOAN"
    LOAN_REPAYMENT = "LOAN_REPAYMENT"
    OVERDRAFT_INTEREST = "OVERDRAFT_INTEREST"

# --------------------------Error Classes --------------------------
# ---------Raise these for the GUI to react appropriately.----------
class ProtocolError(Exception):
    """The server sent something that cannot be parsed."""


class RequestFailed(Exception):
    """The server rejected the request. ``str(self)`` is safe to show to the user."""


# Same class as ProtocolCodex in server/server.py (the client and the server are separate
# programs, so each has its own copy). Only write_action/write_actions are left out: they are
# server-only. Keep the two copies identical.
class ProtocolCodex:
    @dataclass
    class ReadResult[T]:
        read_value: T
        bytes_read: int

    class ActionType(Enum):
        REGISTER = 0
        LOGIN = 1
        DEPOSIT = 2
        WITHDRAWAL = 3
        TRANSFER = 4
        STANDING_ORDER = 5
        CANCEL_STANDING_ORDER = 6
        LOAN = 7
        SHOW_ALL_PAST_ACTIONS = 8
        SHOW_ACTIVE_STANDING_ORDERS = 9
        SHOW_ACTIVE_LOANS = 10
        GET_BALANCE = 11
        LOGOUT = 12

        @classmethod
        def from_bytes(cls, data: bytes) -> Optional[ProtocolCodex.ReadResult[ProtocolCodex.ActionType]]:
            if len(data) < 1:
                return None

            action_value = struct.unpack("!B", data[:1])[0]
            try:
                return ProtocolCodex.ReadResult(read_value=cls(action_value), bytes_read=1)
            except ValueError:
                # unknown action type
                return None

        def to_bytes(self) -> bytes:
            return struct.pack("!B", self.value)

    @staticmethod
    def read_string(data: bytes) -> Optional[ReadResult[str]]:
        if len(data) < 4:
            return None

        str_len = struct.unpack("!I", data[:4])[0]
        if len(data[4:]) < str_len:
            return None

        st = data[4 : 4 + str_len].decode()
        return ProtocolCodex.ReadResult(
            read_value=st,
            bytes_read=4 + str_len
        )

    @staticmethod
    def write_string(st: str) -> bytes:
        st_bytes = st.encode()
        length_bytes = struct.pack("!I", len(st_bytes))
        return length_bytes + st_bytes

    @staticmethod
    def read_float(data: bytes) -> Optional[ReadResult[float]]:
        if len(data) < 8:
            return None
        read_float = struct.unpack("!d", data[:8])[0]
        return ProtocolCodex.ReadResult(
            read_value=read_float,
            bytes_read=8
        )

    @staticmethod
    def write_float(f: float) -> bytes:
        return struct.pack("!d", f)

    @staticmethod
    def write_bool(b: bool) -> bytes:
        return struct.pack("!?", b)

    @staticmethod
    def read_int(data: bytes) -> Optional[ReadResult[int]]:
        if len(data) < 4:
            return None
        i = struct.unpack('!i', data[:4])[0]
        return ProtocolCodex.ReadResult(
            read_value=i,
            bytes_read=4
        )

    @staticmethod
    def write_unsigned_int(i: int) -> bytes:
        return struct.pack("!I", i)

    @staticmethod
    def read_bool(data: bytes) -> Optional[ReadResult[bool]]:
        if len(data) < 1:
            return None
        b = struct.unpack("!?", data[:1])[0]
        return ProtocolCodex.ReadResult(
            read_value=b,
            bytes_read=1
        )

    @staticmethod
    def read_unsigned_int(data: bytes) -> Optional[ReadResult[int]]:
        if len(data) < 4:
            return None
        i = struct.unpack("!I", data[:4])[0]
        return ProtocolCodex.ReadResult(
            read_value=i,
            bytes_read=4
        )

    @staticmethod
    def write_int(i: int) -> bytes:
        return struct.pack("!i", i)

    @staticmethod
    def read_action(data: bytes) -> Optional[ReadResult[tuple]]:
        # an action is 7 strings (id, type, date, from client, from name, to client, to name)
        # and a float (amount)
        fields = []
        bytes_read = 0
        for _ in range(7):
            string_read_result = ProtocolCodex.read_string(data[bytes_read:])
            if string_read_result is None:
                return None
            fields.append(string_read_result.read_value)
            bytes_read += string_read_result.bytes_read
        amount_read_result = ProtocolCodex.read_float(data[bytes_read:])
        if amount_read_result is None:
            return None
        fields.append(amount_read_result.read_value)
        return ProtocolCodex.ReadResult(
            read_value=tuple(fields),
            bytes_read=bytes_read + amount_read_result.bytes_read
        )

    @staticmethod
    def read_actions(data: bytes) -> Optional[ReadResult[list]]:
        count_read_result = ProtocolCodex.read_unsigned_int(data)
        if count_read_result is None:
            return None
        actions = []
        bytes_read = count_read_result.bytes_read
        for _ in range(count_read_result.read_value):
            action_read_result = ProtocolCodex.read_action(data[bytes_read:])
            if action_read_result is None:
                return None
            actions.append(action_read_result.read_value)
            bytes_read += action_read_result.bytes_read
        return ProtocolCodex.ReadResult(
            read_value=actions,
            bytes_read=bytes_read
        )


### Do not remove any of the existing functions or change their signatures. ###
### You can add new functions and classes if needed. ###
class Connection:
    """One TCP connection to the bank server.

    Not thread-safe: call from a single thread (the GUI network worker, or a shell).
    """

    def __init__(self):
        self.sock: Optional[socket.socket] = None
        self.received_bytes = b''
        # the logged-in client, used to tell incoming from outgoing transfers in the history
        self.client_id: Optional[str] = None

    @property
    def connected(self) -> bool:
        """Returns True if a TCP connection to the server is currently open."""
        # a closed socket has fileno -1
        return self.sock is not None and self.sock.fileno() != -1

    def connect(self, host: str, port: int) -> None:
        """Open a TCP connection to the bank server.

        Args:
            host: Server hostname or IP (e.g. ``\"127.0.0.1\"``).
            port: Server TCP port (e.g. ``5050``).
            timeout: Seconds to wait for the TCP handshake.

        Returns:
            None.

        Raises:
            OSError / ConnectionError: if the server cannot be reached.
        """
        self.close()
        new_sock = socket.socket()
        try:
            new_sock.connect((host, port))
        except OSError:
            new_sock.close()
            raise
        self.sock = new_sock

    def close(self) -> None:
        """Close the TCP connection if one is open. Safe to call more than once.

        Returns:
            None.
        """
        # the closed socket object is kept, so a request after close raises OSError
        # (which the GUI handles) instead of failing on None
        if self.sock is not None:
            self.sock.close()
        self.received_bytes = b''
        self.client_id = None

    def receive(self, read_function: Callable):
        """Receive until read_function (a ProtocolCodex reader) can read a whole value, and return it."""
        result = read_function(self.received_bytes)
        while result is None:
            data = self.sock.recv(4096)
            if not data:
                raise ConnectionError("the server closed the connection")
            self.received_bytes += data
            result = read_function(self.received_bytes)
        self.received_bytes = self.received_bytes[result.bytes_read:]
        return result.read_value

    def req_register(self, user: str, password: str) -> str:
        """Register a new account.

        Args:
            user: Account name (3-20 letters/digits/``_``). Need not be unique.
            password: Password (at least 4 characters).

        Returns:
            The new unique user id (token), a 10-character ``A-Z0-9`` string.

        Raises:
            RequestFailed: registration rejected (bad username/password, …).
            ConnectionError: connection lost.
        """
        self.sock.sendall(ProtocolCodex.ActionType.REGISTER.to_bytes() +
                          ProtocolCodex.write_string(user) +
                          ProtocolCodex.write_string(password))
        client_id = self.receive(ProtocolCodex.read_string)
        # the server sends "" when the name or password breaks the rules
        if client_id == "":
            raise RequestFailed("The name must be 3-20 letters, digits or _ "
                                "and the password at least 4 characters")
        return client_id

    def req_login(self, token: str, password: str) -> str:
        """Log in to an existing account on this connection.

        Args:
            token: User id (account id) issued at registration.
            password: Account password.

        Returns:
            The account name (username) for this user id.

        Raises:
            RequestFailed: wrong credentials / unknown user id.
            ConnectionError: connection lost.
        """
        self.sock.sendall(ProtocolCodex.ActionType.LOGIN.to_bytes() +
                          ProtocolCodex.write_string(token) +
                          ProtocolCodex.write_string(password))
        # Bank.LoginResult on the server: 0 = success, 1 = wrong password, 2 = unknown client id
        login_result = self.receive(ProtocolCodex.read_unsigned_int)
        if login_result == 1:
            raise RequestFailed("Wrong password")
        if login_result != 0:
            raise RequestFailed("Unknown user id")
        self.client_id = token
        return self.receive(ProtocolCodex.read_string)

    def req_logout(self) -> None:
        """Log out and close the connection.

        Returns:
            None.
        """
        try:
            if self.connected:
                self.sock.sendall(ProtocolCodex.ActionType.LOGOUT.to_bytes())
        finally:
            self.close()

    def req_get_balance(self) -> str:
        """Fetch the logged-in account's balance.

        Returns:
            Balance string with two decimals, e.g. ``\"500.00\"`` or ``\"-12.50\"``.

        Raises:
            RequestFailed: not logged in or server error.
            ConnectionError: connection lost.
        """
        self.sock.sendall(ProtocolCodex.ActionType.GET_BALANCE.to_bytes())
        balance = self.receive(ProtocolCodex.read_float)
        return f"{balance:.2f}"

    def req_deposit(self, amount: str) -> str:
        """Deposit cash into the logged-in account.

        Args:
            amount: Positive decimal string, at most 2 fractional digits (e.g. ``\"99.90\"``).

        Returns:
            New balance string (two decimals).

        Raises:
            RequestFailed: invalid amount or not logged in.
            ConnectionError: connection lost.
        """
        # the GUI passes the text the user typed; a ValueError would stop the GUI's network thread
        try:
            amount_value = float(amount)
        except ValueError:
            raise RequestFailed("Amount must be a number") from None
        self.sock.sendall(ProtocolCodex.ActionType.DEPOSIT.to_bytes() +
                          ProtocolCodex.write_float(amount_value))
        if not self.receive(ProtocolCodex.read_bool):
            raise RequestFailed("Deposit failed")
        return self.req_get_balance()

    def req_withdraw(self, amount: str) -> str:
        """Withdraw cash from the logged-in account (may go negative).

        Args:
            amount: Positive decimal string, at most 2 fractional digits.

        Returns:
            New balance string (two decimals).

        Raises:
            RequestFailed: invalid amount or not logged in.
            ConnectionError: connection lost.
        """
        try:
            amount_value = float(amount)
        except ValueError:
            raise RequestFailed("Amount must be a number") from None
        self.sock.sendall(ProtocolCodex.ActionType.WITHDRAWAL.to_bytes() +
                          ProtocolCodex.write_float(amount_value))
        if not self.receive(ProtocolCodex.read_bool):
            raise RequestFailed("Withdrawal failed")
        return self.req_get_balance()

    def req_transfer(self, target: str, amount: str) -> str:
        """Transfer money to another account.

        Args:
            target: Recipient user id (token).
            amount: Positive decimal string, at most 2 fractional digits.

        Returns:
            New balance of the sender (two decimals).

        Raises:
            RequestFailed: bad amount, unknown target, self-transfer, …
            ConnectionError: connection lost.
        """
        try:
            amount_value = float(amount)
        except ValueError:
            raise RequestFailed("Amount must be a number") from None
        self.sock.sendall(ProtocolCodex.ActionType.TRANSFER.to_bytes() +
                          ProtocolCodex.write_string(target) +
                          ProtocolCodex.write_float(amount_value))
        if not self.receive(ProtocolCodex.read_bool):
            raise RequestFailed("Transfer failed")
        return self.req_get_balance()

    def req_schedule_so(self, target: str, amount: str, count: str) -> int:
        """Create a monthly standing order (first payment today).

        Args:
            target: Payee user id (token).
            amount: Amount per payment (positive decimal string).
            count: Number of payments as a digit string; ``\"0\"`` means until cancelled.

        Returns:
            The new standing-order id (positive int).

        Raises:
            RequestFailed: validation error or unknown target.
            ConnectionError: connection lost.
        """
        try:
            amount_value = float(amount)
            count_value = int(count)
        except ValueError:
            raise RequestFailed("Amount must be a number and the number of payments a whole number") from None
        self.sock.sendall(ProtocolCodex.ActionType.STANDING_ORDER.to_bytes() +
                          ProtocolCodex.write_string(target) +
                          ProtocolCodex.write_float(amount_value) +
                          ProtocolCodex.write_int(count_value))
        # the new standing order's id, "" if it failed
        standing_order_id = self.receive(ProtocolCodex.read_string)
        if standing_order_id == "":
            raise RequestFailed("Standing order failed")
        return standing_order_id

    def req_get_so_list(self) -> List[Tuple[int, str, str, str, int, Optional[int], str]]:
        """List active standing orders paid by the logged-in account.

        Returns:
            A list of tuples, one per standing order, in any order::

                (so_id, counterparty_token, counterparty_name,
                 amount, executed, remaining, next_run)

            * ``so_id`` (int)
            * ``counterparty_token`` (str) — payee user id
            * ``counterparty_name`` (str) — payee account name
            * ``amount`` (str) — per-payment amount, two decimals
            * ``executed`` (int) — how many payments have run
            * ``remaining`` (int | None) — payments left, or ``None`` if until cancelled
            * ``next_run`` (str) — next payment date ``YYYY-MM-DD``

        Raises:
            RequestFailed: not logged in or server error.
            ConnectionError: connection lost.
        """
        self.sock.sendall(ProtocolCodex.ActionType.SHOW_ACTIVE_STANDING_ORDERS.to_bytes())
        # each standing order is an action, then the payments left (-1 = until cancelled)
        # and the next payment date
        number_of_standing_orders = self.receive(ProtocolCodex.read_unsigned_int)
        standing_orders = []
        for _ in range(number_of_standing_orders):
            action_id, action_type, action_date, from_client, from_name, to_client, to_name, amount = \
                self.receive(ProtocolCodex.read_action)
            remaining = self.receive(ProtocolCodex.read_int)
            next_payment_date = self.receive(ProtocolCodex.read_string)
            # the number of payments already made isn't stored on the server
            standing_orders.append((action_id, to_client, to_name, f"{amount:.2f}", "-",
                                    None if remaining == -1 else remaining, next_payment_date))
        return standing_orders

    def req_cancel_so(self, so_id: Union[str, int]) -> None:
        """Cancel one of the logged-in account's standing orders.

        Args:
            so_id: Standing-order id (int or digit string).

        Returns:
            None on success.

        Raises:
            RequestFailed: unknown standing order or not owned by this account.
            ConnectionError: connection lost.
        """
        self.sock.sendall(ProtocolCodex.ActionType.CANCEL_STANDING_ORDER.to_bytes() +
                          ProtocolCodex.write_string(str(so_id)))
        if not self.receive(ProtocolCodex.read_bool):
            raise RequestFailed("Cancelling the standing order failed")

    def req_loan(self, amount: str) -> Tuple[int, str]:
        """Take a loan (principal credited now; auto-repay after 30 bank days + 5%).

        Args:
            amount: Principal as a positive decimal string.

        Returns:
            ``(loan_id, new_balance)`` where ``loan_id`` is int and ``new_balance``
            is a two-decimal string.

        Raises:
            RequestFailed: invalid amount or not logged in.
            ConnectionError: connection lost.
        """
        try:
            amount_value = float(amount)
        except ValueError:
            raise RequestFailed("Amount must be a number") from None
        self.sock.sendall(ProtocolCodex.ActionType.LOAN.to_bytes() +
                          ProtocolCodex.write_float(amount_value))
        # the new loan's id, "" if it was refused
        loan_id = self.receive(ProtocolCodex.read_string)
        if loan_id == "":
            raise RequestFailed("Loan refused")
        return loan_id, self.req_get_balance()

    def req_get_loans(self) -> List[Tuple[int, str, str, str, str]]:
        """List unpaid loans for the logged-in account.

        Returns:
            A list of tuples, one per unpaid loan, in any order::

                (loan_id, principal, repay_amount, taken_on, due_on)

            * ``loan_id`` (int)
            * ``principal`` (str) — two decimals
            * ``repay_amount`` (str) — principal * 1.05, two decimals
            * ``taken_on`` (str) — ``YYYY-MM-DD``
            * ``due_on`` (str) — ``YYYY-MM-DD``

        Raises:
            RequestFailed: not logged in or server error.
            ConnectionError: connection lost.
        """
        self.sock.sendall(ProtocolCodex.ActionType.SHOW_ACTIVE_LOANS.to_bytes())
        loans = self.receive(ProtocolCodex.read_actions)
        # each unpaid loan is its pending repayment: amount = principal * 1.05, date = the day it was taken
        return [(action_id, f"{amount / 1.05:.2f}", f"{amount:.2f}", taken_on,
                 (date.fromisoformat(taken_on) + timedelta(days=30)).isoformat())
                for action_id, action_type, taken_on, from_client, from_name, to_client, to_name, amount in loans]

    def req_history(self, from_ts: Optional[float] = None,
                    to_ts: Optional[float] = None) -> List[Tuple[str, TxType, str, str, str, str, str]]:
        """Fetch account history (oldest first). The GUI reverses for display.

        Args:
            from_ts: Optional inclusive lower bound (Unix seconds). The GUI passes
                neither bound (full history).
            to_ts: Optional inclusive upper bound (Unix seconds).

        Returns:
            A list of tuples, **oldest first**::

                (time, type, counterparty, counterparty_name, amount, balance, note)

            * ``time`` (str) — e.g. ``\"2026-10-30 14:13:20\"``
            * ``type`` (``TxType``) — e.g. ``TxType.DEPOSIT``, ``TxType.TRANSFER_IN``, …
            * ``counterparty`` (str) — other party token, ``CASH``, or ``BANK``
            * ``counterparty_name`` (str) — display name (may equal counterparty)
            * ``amount`` (str) — signed two-decimal amount for this entry
            * ``balance`` (str) — balance after this entry, two decimals
            * ``note`` (str) — free text (may be empty)

        Raises:
            RequestFailed: not logged in or bad timestamps.
            ConnectionError: connection lost.
        """
        self.sock.sendall(ProtocolCodex.ActionType.SHOW_ALL_PAST_ACTIONS.to_bytes())
        past_actions = self.receive(ProtocolCodex.read_actions)

        history = []
        # every change to the balance is in the history, so the balance after each entry
        # is the sum of the amounts up to it
        balance = 0.0
        # the server sends newest first, the GUI wants oldest first
        for action_id, action_type, action_time, from_client, from_name, to_client, to_name, amount \
                in reversed(past_actions):
            outgoing = from_client == self.client_id
            match action_type:
                case "DEPOSIT":
                    tx_type, counterparty, counterparty_name, signed_amount = TxType.DEPOSIT, "CASH", "CASH", amount
                case "WITHDRAWAL":
                    tx_type, counterparty, counterparty_name, signed_amount = TxType.WITHDRAWAL, "CASH", "CASH", -amount
                case "TRANSFER":
                    tx_type = TxType.TRANSFER_OUT if outgoing else TxType.TRANSFER_IN
                    counterparty = to_client if outgoing else from_client
                    counterparty_name = to_name if outgoing else from_name
                    signed_amount = -amount if outgoing else amount
                case "STANDING_ORDER":
                    tx_type = TxType.CHARGE
                    counterparty = to_client if outgoing else from_client
                    counterparty_name = to_name if outgoing else from_name
                    signed_amount = -amount if outgoing else amount
                case "TAKE_LOAN":
                    tx_type, counterparty, counterparty_name, signed_amount = TxType.LOAN, "BANK", "BANK", amount
                case "LOAN_RETURN":
                    tx_type, counterparty, counterparty_name, signed_amount = \
                        TxType.LOAN_REPAYMENT, "BANK", "BANK", -amount
                case "MINUS":
                    tx_type, counterparty, counterparty_name, signed_amount = \
                        TxType.OVERDRAFT_INTEREST, "BANK", "BANK", -amount
                case _:
                    raise ProtocolError(f"unknown action type {action_type!r}")
            balance += signed_amount
            # "2026-01-31T14:13:20.123456" -> "2026-01-31 14:13:20"
            time_text = action_time.replace("T", " ")[:19]
            history.append((time_text, tx_type, counterparty, counterparty_name,
                            f"{signed_amount:.2f}", f"{balance:.2f}", ""))
        return history
