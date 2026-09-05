# http-load-tester domain context

This context defines the vocabulary used by the raw HTTP load tester when it
describes generated work, protocol results, and resource boundaries.

## Workload

**Attempt**:
One scheduled request execution, whether it ends in a valid HTTP response, a
transport or protocol failure, a timeout, or cancellation.
_Avoid_: request count when referring to one execution.

**Test plan**:
The immutable, validated description of one load-test run before any socket
is opened.
_Avoid_: configuration when referring to the complete executable plan.

**Closed-loop**:
A workload model in which a worker schedules its next attempt only after its
previous attempt completes.
_Avoid_: constant-rate when referring to this model.

**Open-loop**:
A workload model in which attempts are scheduled against a monotonic timeline
without waiting for earlier attempts to complete.
_Avoid_: concurrent loop.

## Protocol and resource boundaries

**Origin**:
The connection identity formed by a scheme, hostname, port, and TLS identity
settings.
_Avoid_: host when TLS settings or the port affect connection identity.

**HTTP error**:
A valid HTTP response whose status code is in the client-defined error range;
it is not a transport or protocol failure.
_Avoid_: request failure for a response such as 404 or 500.

**Pool wait**:
The elapsed time an attempt spends waiting to acquire an available connection
from the bounded origin pool.
_Avoid_: request latency; pool wait is reported separately.

**Reusable connection**:
A session whose complete response has been consumed and whose framing and
headers permit another sequential HTTP/1.1 exchange.
_Avoid_: keep-alive connection when reusability has not been established.
