Bank protocol

The client and the server talk over TCP, one connection per client.
The client sends a request and waits for the response before it sends the next one.
Every request starts with 1 byte - type of action, and then the fields of that action.

How fields are sent:
all numbers are big endian (network byte order)  
string - 4 bytes unsigned int with the length in bytes, then the string in utf-8  
float - 8 bytes (double)  
int - 4 bytes signed  
unsigned int - 4 bytes unsigned  
bool - 1 byte, 0 is false and 1 is true

Types of actions and their 1-byte encodings:  
0 - register  
1 - login  
2 - deposit  
3 - withdrawal  
4 - transfer  
5 - standing order  
6 - cancel standing order  
7 - loan  
8 - show all past actions  
9 - show active standing orders  
10 - show active loans  
11 - get balance  
12 - logout  

Login and session:  
Before login the client can only send register and login.  
If it sends anything else, the server closes the connection.  
After a successful login the server remembers the client id for this connection,
so the other requests don't send the client id.  
A failed login means the client is not logged in.  
After register the client still has to log in.  
If the server gets a type of action it doesn't know, it closes the connection.

Register request:  
1 byte - type of action (0)  
string - name  
string - password  

Register response:  
string - the new client id (10 characters, A-Z and 0-9)  
the string is empty if the name is not 3-20 letters, digits or _, or the password is shorter than 4 characters  

Login request:  
1 byte - type of action (1)  
string - client id  
string - password  

Login response:  
4 bytes unsigned int - result: 0 success, 1 wrong password, 2 unknown client id  
only if the result is 0: string - the name of the client  

Deposit request:  
1 byte - type of action (2)  
float - amount  

Deposit response:  
bool - true if it worked (false if the amount is not positive)  

Withdrawal request:  
1 byte - type of action (3)  
float - amount  

Withdrawal response:  
bool - true if it worked (false if the amount is not positive)  
the balance can go below 0  

Transfer request:  
1 byte - type of action (4)  
string - client id to transfer to  
float - amount  

Transfer response:  
bool - true if it worked (false if the amount is not positive, the other client doesn't exist or it is your own id)  

Standing order request:  
1 byte - type of action (5)  
string - client id to pay to  
float - amount of every payment  
int - number of payments, 0 means until cancelled  

Standing order response:  
string - id of the standing order, empty if it failed (the amount is not positive, the other client doesn't exist or it is your own id)  
the first payment happens right away, then once a month on the same day.  
if a month doesn't have that day, the payment is on the last day of the month (jan 31 -> feb 28 -> mar 31)  

Cancel standing order request:  
1 byte - type of action (6)  
string - id of the standing order (the id from show active standing orders)  

Cancel standing order response:  
bool - true if it was cancelled  

Loan request:  
1 byte - type of action (7)  
float - amount  

Loan response:  
string - id of the loan, empty if it was refused (the amount is not positive or the balance is below 0)  
the loan is paid back automatically after 30 days with 5% interest  

Show all past actions request:  
1 byte - type of action (8)  

Show all past actions response:  
list of actions, newest first  

Show active standing orders request:  
1 byte - type of action (9)  

Show active standing orders response:  
4 bytes unsigned int - number of standing orders, then for every standing order:  
action - the last payment of the standing order (its id is the one to cancel)  
int - payments left, -1 means until cancelled  
string - date of the next payment (YYYY-MM-DD)  

Show active loans request:  
1 byte - type of action (10)  

Show active loans response:  
list of actions, newest first  
every action is the payment back of a loan: the amount is what will be paid back (the loan + 5%)  
and the date is the day the loan was taken. it is paid back 30 days after that date  

Get balance request:  
1 byte - type of action (11)  

Get balance response:  
float - balance  

Logout request:  
1 byte - type of action (12)  

Logout response:  
no response, the server closes the connection  

Action (used in the lists):  
string - action id  
string - type: DEPOSIT, WITHDRAWAL, TRANSFER, STANDING_ORDER, TAKE_LOAN, LOAN_RETURN, MINUS (daily interest when the balance is below 0)  
string - date: YYYY-MM-DDTHH:MM:SS.ffffff when it was done, or YYYY-MM-DD if it didn't happen yet  
string - client id it came from  
string - name of that client  
string - client id it went to, empty if there is none  
string - name of that client, empty if there is none  
float - amount  

List of actions:  
4 bytes unsigned int - number of actions  
then the actions one after another  

Why we chose this:  
Everything is binary and every string has its length before it, so the receiver always knows how many
bytes it still needs. That way reading works even if TCP splits a message into pieces or puts two messages
together, and we don't need any special characters to mark where a message ends.
Numbers are big endian so it works the same between different computers.
The first byte of a request tells the server how to read the rest of it. If the server doesn't know the type,
it can't know where the next request starts, so it closes the connection.
The client logs in once per connection and the server remembers who it is. So the client id isn't sent with
every request, and nobody can do actions on an account without its password.
The client waits for the response of every request before sending the next one, and TCP keeps the order,
so we don't need request ids to match responses to requests.
All three lists use the same action format, so one reader and one writer are enough for all of them.
