"""Pure decision policies (rule 6 of ``docs/architecture/state-and-boundaries.md``).

Every module here is a function from values to a decision: no IO, no clock
reads, no sleeps, no database. The IO caller gathers the inputs, calls the
policy and acts on the decision it gets back. Tests drive each policy as a
table over its inputs.

* ``admission``      which origin may wake an agent, and how (``turn_dispatch``)
* ``notifications``  whether a completed turn pages the user (``user_notifications``)
* ``failover``       what to do with parked Claude work (``claude_failover``)
* ``agent_spec``     how an agent create/relaunch request is parsed (``agent_lifecycle``)
"""
