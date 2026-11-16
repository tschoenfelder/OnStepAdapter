# OnStepAdapter 0.6.3

This patch release raises the maximum finite RA/DEC axis jog from 10 to 15
degrees. The configured projected-altitude and DEC target limits introduced in
0.6.2 remain mandatory and are checked before any command is sent.

Requests at exactly 15 degrees are accepted; requests above 15 degrees are
rejected without mount commands.

Property waiters also consume a fresh acknowledgement that arrived immediately
before an INDI peer EOF. If no newer property arrived, reader failure continues
to be reported promptly with its original cause.
