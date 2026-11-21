"""Single-threaded TCP banking server multiplexed with ``select()``.

Usage:
    python server.py [--host 127.0.0.1] [--port 5050] [--start-date YYYY-MM-DD] [--verbose]
"""
from __future__ import annotations

import argparse
import logging
import socket
from datetime import date
from enum import Enum, nonmember

from typing import Optional, List, Dict
from dataclasses import dataclass
from bank import Bank, Action, StandingOrder, ReturnLoan, add_one_month
from server_utils import Console
import struct

import select

log = logging.getLogger("server")



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

    @staticmethod
    def write_action(action: Action, bank: Bank) -> bytes:
        action_date = action._done_at or action._date_created
        to_client = getattr(action, "_to_client", "")
        return (ProtocolCodex.write_string(action.action_id) +
                ProtocolCodex.write_string(action.action_type()) +
                ProtocolCodex.write_string(action_date.isoformat()) +
                ProtocolCodex.write_string(action._created_by_client) +
                ProtocolCodex.write_string(bank.get_name(action._created_by_client)) +
                ProtocolCodex.write_string(to_client) +
                ProtocolCodex.write_string(bank.get_name(to_client) if to_client else "") +
                ProtocolCodex.write_float(action._amount))

    @staticmethod
    def write_actions(actions: List[Action], bank: Bank) -> bytes:
        actions_bytes = ProtocolCodex.write_unsigned_int(len(actions))
        for action in actions:
            actions_bytes += ProtocolCodex.write_action(action, bank)
        return actions_bytes


class ProtocolImplementer:
    def __init__(self, bank:Bank):
        self.received_bytes = b''
        self.bytes_to_send = b''
        self.bank = bank
        # set by a successful login; every request except register and login needs it
        self.client_id: Optional[str] = None
        # tells the server to close this connection (logout, or a request before login)
        self.should_close: bool = False

    def handle_register(self):
        name_read_result = ProtocolCodex.read_string(self.received_bytes[1:])
        if name_read_result is None:
            return False

        password_read_result = ProtocolCodex.read_string(self.received_bytes[1 + name_read_result.bytes_read:])
        if password_read_result is None:
            return False

        registered_client_id = self.bank.register_client(
            name=name_read_result.read_value,
            password=password_read_result.read_value
        )

        self.bytes_to_send += ProtocolCodex.write_string(registered_client_id)
        self.received_bytes = self.received_bytes[1 + name_read_result.bytes_read + password_read_result.bytes_read:]
        return True

    def handle_login(self):
        client_id_read_result = ProtocolCodex.read_string(self.received_bytes[1:])
        if client_id_read_result is None:
            return False

        password_read_result = ProtocolCodex.read_string(self.received_bytes[1 + client_id_read_result.bytes_read:])
        if password_read_result is None:
            return False

        login_result = self.bank.login(
            client_id=client_id_read_result.read_value,
            password=password_read_result.read_value
        )
        if login_result == Bank.LoginResult.SUCCESS:
            self.client_id = client_id_read_result.read_value
        else:
            self.client_id = None

        self.bytes_to_send += ProtocolCodex.write_unsigned_int(login_result.value)
        if self.client_id is not None:
            # on success the client also gets the account name
            self.bytes_to_send += ProtocolCodex.write_string(self.bank.get_name(self.client_id))
        self.received_bytes = self.received_bytes[1+client_id_read_result.bytes_read+password_read_result.bytes_read:]
        return True

    def handle_deposit(self):
        deposit_amount = ProtocolCodex.read_float(self.received_bytes[1:])
        if deposit_amount is None:
            return False
        deposit_result = self.bank.deposit(client_id=self.client_id,
                                           amount=deposit_amount.read_value)
        self.bytes_to_send+=ProtocolCodex.write_bool(deposit_result)
        self.received_bytes = self.received_bytes[1+deposit_amount.bytes_read:]
        return True

    def handle_withdrawal(self):
        withdrawal_amount = ProtocolCodex.read_float(self.received_bytes[1:])
        if withdrawal_amount is None:
            return False
        withdrawal_result = self.bank.withdraw(client_id=self.client_id,
                                               amount=withdrawal_amount.read_value)
        self.bytes_to_send += ProtocolCodex.write_bool(withdrawal_result)
        self.received_bytes = self.received_bytes[1 + withdrawal_amount.bytes_read:]
        return True

    def handle_transfer(self):
        to_client_id_read_result = ProtocolCodex.read_string(self.received_bytes[1:])
        if to_client_id_read_result is None:
            return False
        transfer_amount = ProtocolCodex.read_float(self.received_bytes[1 + to_client_id_read_result.bytes_read:])
        if transfer_amount is None:
            return False
        transfer_result = self.bank.transfer(from_client_id=self.client_id,
                                             to_client_id=to_client_id_read_result.read_value,
                                             amount=transfer_amount.read_value)
        self.bytes_to_send += ProtocolCodex.write_bool(transfer_result)
        self.received_bytes = self.received_bytes[1 + to_client_id_read_result.bytes_read+transfer_amount.bytes_read:]
        return True

    def handle_standing_order(self):
        to_client_id_read_result = ProtocolCodex.read_string(self.received_bytes[1:])
        if to_client_id_read_result is None:
            return False
        amount_read_result = ProtocolCodex.read_float(
            self.received_bytes[1 + to_client_id_read_result.bytes_read:])
        if amount_read_result is None:
            return False
        number_of_payments = ProtocolCodex.read_int(
            self.received_bytes[1 + to_client_id_read_result.bytes_read + amount_read_result.bytes_read:]
        )
        if number_of_payments is None:
            return False

        # the new standing order's id, "" if it failed
        created_standing_order_id = self.bank.create_standing_order(
            from_client_id=self.client_id,
            to_client_id=to_client_id_read_result.read_value,
            amount=amount_read_result.read_value,
            number_of_payments=number_of_payments.read_value if number_of_payments.read_value > 0 else None
        )
        self.bytes_to_send += ProtocolCodex.write_string(created_standing_order_id)
        self.received_bytes = self.received_bytes[1 + to_client_id_read_result.bytes_read + amount_read_result.bytes_read +
                                                  number_of_payments.bytes_read:]
        return True

    def handle_cancel_standing_order(self):
        standing_order_id_read_result = ProtocolCodex.read_string(self.received_bytes[1:])
        if standing_order_id_read_result is None:
            return False
        cancel_result = self.bank.cancel_standing_order(client_id=self.client_id,
                                                        standing_order_id=standing_order_id_read_result.read_value)
        self.bytes_to_send += ProtocolCodex.write_bool(cancel_result)
        self.received_bytes = self.received_bytes[1 + standing_order_id_read_result.bytes_read:]
        return True

    def handle_loan(self):
        loan_amount = ProtocolCodex.read_float(self.received_bytes[1:])
        if loan_amount is None:
            return False
        # the new loan's id, "" if it failed
        loan_id = self.bank.loan(client_id=self.client_id,
                                 amount=loan_amount.read_value)
        self.bytes_to_send += ProtocolCodex.write_string(loan_id)
        self.received_bytes = self.received_bytes[1 + loan_amount.bytes_read:]
        return True

    def handle_show_all_past_actions(self):
        past_actions = self.bank.get_past_actions(client_id=self.client_id)
        all_past_actions = sorted(past_actions.incoming_actions | past_actions.outgoing_actions,
                                  key=lambda action: action._done_at, reverse=True)
        self.bytes_to_send += ProtocolCodex.write_actions(all_past_actions, self.bank)
        self.received_bytes = self.received_bytes[1:]
        return True

    def handle_show_active_standing_orders(self):
        active_actions = self.bank.get_active_actions(client_id=self.client_id)
        active_standing_orders = sorted([action for action in active_actions if isinstance(action, StandingOrder)],
                                        key=lambda action: action._done_at, reverse=True)
        # like write_actions, but each standing order also has the payments left and the next payment date
        standing_orders_bytes = ProtocolCodex.write_unsigned_int(len(active_standing_orders))
        for standing_order in active_standing_orders:
            # -1 means until cancelled
            remaining = standing_order._number_of_payments - 1 if standing_order._number_of_payments is not None else -1
            next_payment_date = add_one_month(standing_order._done_at.date(), standing_order._day_of_month)
            standing_orders_bytes += (ProtocolCodex.write_action(standing_order, self.bank) +
                                      ProtocolCodex.write_int(remaining) +
                                      ProtocolCodex.write_string(next_payment_date.isoformat()))
        self.bytes_to_send += standing_orders_bytes
        self.received_bytes = self.received_bytes[1:]
        return True

    def handle_show_active_loans(self):
        active_actions = self.bank.get_active_actions(client_id=self.client_id)
        # a loan was taken when the TakeLoan action that created its ReturnLoan was done
        active_loans = sorted([action for action in active_actions if isinstance(action, ReturnLoan)],
                              key=lambda loan: self.bank.actions[loan._created_by_action]._done_at, reverse=True)
        self.bytes_to_send += ProtocolCodex.write_actions(active_loans, self.bank)
        self.received_bytes = self.received_bytes[1:]
        return True

    def handle_get_balance(self):
        balance = self.bank.get_balance(client_id=self.client_id)
        self.bytes_to_send += ProtocolCodex.write_float(balance)
        self.received_bytes = self.received_bytes[1:]
        return True

    def handle_logout(self):
        self.client_id = None
        self.should_close = True
        self.received_bytes = self.received_bytes[1:]
        return True

    def handle_received_bytes(self, new_bytes: bytes):
        self.received_bytes += new_bytes

        handled: bool = True
        while len(self.received_bytes) > 0 and not self.should_close:
            action_result = ProtocolCodex.ActionType.from_bytes(self.received_bytes)
            # the loop makes sure there is at least 1 byte, so None means an unknown action type
            if action_result is None:
                self.should_close = True
                return
            action_type = action_result.read_value

            # only register and login are allowed before the client logs in
            if action_type not in (ProtocolCodex.ActionType.REGISTER, ProtocolCodex.ActionType.LOGIN) \
                    and self.client_id is None:
                self.should_close = True
                return

            match action_type:
                case ProtocolCodex.ActionType.REGISTER:
                    handled = self.handle_register()
                case ProtocolCodex.ActionType.LOGIN:
                    handled = self.handle_login()
                case ProtocolCodex.ActionType.DEPOSIT:
                    handled = self.handle_deposit()
                case ProtocolCodex.ActionType.WITHDRAWAL:
                    handled = self.handle_withdrawal()
                case ProtocolCodex.ActionType.TRANSFER:
                    handled = self.handle_transfer()
                case ProtocolCodex.ActionType.STANDING_ORDER:
                    handled = self.handle_standing_order()
                case ProtocolCodex.ActionType.CANCEL_STANDING_ORDER:
                    handled = self.handle_cancel_standing_order()
                case ProtocolCodex.ActionType.LOAN:
                    handled = self.handle_loan()
                case ProtocolCodex.ActionType.SHOW_ALL_PAST_ACTIONS:
                    handled = self.handle_show_all_past_actions()
                case ProtocolCodex.ActionType.SHOW_ACTIVE_STANDING_ORDERS:
                    handled = self.handle_show_active_standing_orders()
                case ProtocolCodex.ActionType.SHOW_ACTIVE_LOANS:
                    handled = self.handle_show_active_loans()
                case ProtocolCodex.ActionType.GET_BALANCE:
                    handled = self.handle_get_balance()
                case ProtocolCodex.ActionType.LOGOUT:
                    handled = self.handle_logout()

            if not handled:
                return

    def has_data_to_write(self) -> bool:
        return len(self.bytes_to_send) > 0

    def handle_bytes_sent(self, number_of_bytes_sent: int):
        self.bytes_to_send = self.bytes_to_send[number_of_bytes_sent:]

### Do not remove any of the existing functions or change their signatures. ###
### You can add new functions and classes if needed. ###
class Server:
    def __init__(self, host: str, port: int, bank: Bank):
        self.host = host
        self.port = port
        self.bank = bank
        self.bank.announce_date()

        self.console = Console()

    def server_loop(self) -> None:
        with socket.socket() as listening_socket:
            listening_socket.bind((self.host, self.port))
            listening_socket.listen()
            protocols: Dict[socket.socket, ProtocolImplementer] = {}
            while True:
                rlist = list(protocols.keys())+[listening_socket]
                wlist = [connection for connection, proto in protocols.items() if proto.has_data_to_write()]
                readable,writable,exceptional = select.select(rlist,wlist,protocols.keys(),0.1)
                if listening_socket in readable:
                    new_connection, client_addr = listening_socket.accept()
                    protocols[new_connection] = ProtocolImplementer(self.bank)
                    readable.remove(listening_socket)
                for connection in readable:
                    try:
                        data = connection.recv(4096)
                    except OSError:
                        self.close_connection(connection, protocols)
                        continue
                    if not data:
                        self.close_connection(connection, protocols)
                        continue
                    try:
                        protocols[connection].handle_received_bytes(data)
                    except Exception:
                        # a request the server couldn't handle: drop only this client, keep the server running
                        self.close_connection(connection, protocols)
                        continue
                    if protocols[connection].should_close:
                        self.close_connection(connection, protocols)
                for connection in writable:
                    if connection not in protocols:
                        continue
                    try:
                        bytes_sent = connection.send(protocols[connection].bytes_to_send)
                    except OSError:
                        self.close_connection(connection, protocols)
                        continue
                    protocols[connection].handle_bytes_sent(bytes_sent)
                for connection in exceptional:
                    if connection in protocols:
                        self.close_connection(connection, protocols)
                self.console.poll(self.on_console_line)
                # runs the days that passed (nothing if the date didn't change)
                self.bank.do_today()

    def close_connection(self, connection: socket.socket, protocols: Dict[socket.socket, ProtocolImplementer]):
        connection.close()
        del protocols[connection]

    def on_console_line(self, line: str):
        self.bank.clock.advance_day()
        self.bank.do_today()
        self.bank.announce_date()





### Do not touch anything below this line. ###
def parse_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {text!r}") from None


def main() -> None:
    parser = argparse.ArgumentParser(description="select()-based socket banking server")
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int, default=5050)
    parser.add_argument("--start-date", type=parse_date, default=date.today(),
                        help="first bank date, YYYY-MM-DD (default: today)")
    parser.add_argument("--verbose", action="store_true", help="log every request and reply")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(name)s: %(message)s", datefmt="%H:%M:%S")

    bank = Bank(start_date=args.start_date)
    server = Server(args.host, args.port, bank)
    try:
        server.server_loop()
    except KeyboardInterrupt:
        log.info("shutting down")
        print("\nShutting down.", flush=True)


if __name__ == "__main__":
    main()
