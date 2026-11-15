# Integration handoff — Project 15

These are proposed connection settings. No teammate's app, endpoint or shared GitHub repository was supplied or verified in this run.

## Person 1 — TCP reservation app

- Suggested server location is **h3 (10.0.0.3)**, with h1/h2 as clients. Start programs from the Mininet CLI so they execute in the correct network namespaces.
- Suggested app port is **5000**. The sample benchmark uses 5201 and 5202, so no conflict with this proposed application port.
- Bind the app to `10.0.0.3` or `0.0.0.0` **inside h3**, not the VM root namespace. Confirm entry points, TCP port, message framing, accept/reject response, and how a reservation is released.
- Send Person 2 enough flow information to identify the data transfer, including source IP, destination IP, protocol and destination port. The TCP reservation request itself may not be the flow that should receive reserved bandwidth.
- Proposed demo given a 10 Mbps capacity. h1 requests 6 Mbps, then h2 requests 5 Mbps and is rejected. After h1 releases, h2 retries 5 Mbps. Expected decisions are accept → reject → accept if the actual app uses this capacity and accounting. These are **test inputs and expected outcomes**, not observed results.

## Person 2 — Ryu and dynamic policy

- Confirm IP and port (defaults `127.0.0.1:6653`), OpenFlow 1.3, DPID **1**, and port mapping **1=h1, 2=h2, 3=h3**.
- First verify basic forwarding, then capture the flow state before and after a real accepted request. Confirm what Ryu installs or updates and how rules are released. An accepted app response by itself doesn't prove enforcement.
- For the Person 3 benchmark, keep Ryu in forwarding-only mode. It uses a lab-owned Linux `tc` HTB root qdisc on `s1-eth3`. Do **not** run this local shaper on the same egress at the same time as Person 2's OVS QoS/HTB setup.
- For dynamic integration, have Person 2 own the enforcement. If using OVS QoS, configure a queue on the actual egress and ensure the OpenFlow rule selects it and outputs to the correct port. If using an OpenFlow meter, document it as a rate limiter rather than a minimum-bandwidth guarantee. Agree on policy IDs, rules, and release behavior.
- Use a separate run for the integrated accept/reject/release demo. Record app requests/responses, flow or queue changes, and iPerf3 goodput during competing traffic. The bundled benchmark is a reproducible policy comparison, not a substitute for that end-to-end check.

## What to say in the demo

> Mininet creates three virtual hosts connected to one Open vSwitch. The clients ask the reservation application for bandwidth over TCP. The application decides whether there is enough unreserved capacity. The controller is responsible for installing the policy for an accepted reservation. My module verifies the switch connection, checks that hosts can communicate, and measures what happens when two traffic flows compete for the same link. Both tests use the same 10 Mbps bottleneck, so the difference we're measuring is the scheduling policy, not the link capacity.

For this module's standalone benchmark, say explicitly that Linux HTB applies the example reservation while Ryu handles forwarding. Only describe the dynamic policy as working once the team has tested its actual controller and app together.
