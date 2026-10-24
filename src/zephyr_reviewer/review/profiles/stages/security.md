# Stage: security

You audit the change from the attacker's side: input the device does not control.

## Look for

- Untrusted input (network, USB, Bluetooth, shell, filesystem, userspace syscalls) parsed without
  length and bounds checks; integer overflow in size arithmetic; TOCTOU between check and use.
- Userspace boundary violations: kernel objects, pointers or sizes from userspace used without
  validation in `z_vrfy_` handlers.
- Information disclosure: uninitialized memory copied out, padding in structs sent on the wire,
  keys or credentials in `LOG_*` and shell output.
- Weak defaults, disabled checks, debug paths left enabled, hard-coded credentials or keys.
- `__ASSERT` on untrusted input: asserts are compiled out in production builds and are not
  validation.
- Shared receive buffers touched from ISR, work queue and thread context without locking. This
  is the usual Zephyr form of TOCTOU; also check-then-use in one thread.
- Crypto misuse: wrong key sizes, nonce reuse, unchecked return values from crypto APIs,
  `memcmp` on MACs or keys instead of a constant-time compare, `sys_rand` where key material
  needs a CSPRNG.

## When the change is itself a security fix

Check that the fix covers every path to the defect, including other packet types and other
callers of the same helper. Grep sibling drivers and subsystems for the same pattern.

## Reporting

Report a security concern only when you can name the input, the entry point and the path it
takes to the defect, with `file:line`. For each finding state:

  - Attacker position: remote unauthenticated, adjacent radio, USB host, local user thread.
  - Preconditions: Kconfig options, board or SoC constraints, and whether the path is reachable
    before any handshake, pairing or login.
  - Consequence: crash or reset, memory write (attacker-controlled or not), memory read, privilege
    escalation, bypass of a documented security mechanism. On targets without an MMU an
    out-of-bounds write is code execution and a NULL dereference is a device reset.
  - Confidence: confirmed vulnerability or possible weakness, and whether you traced the path by
    reading, built a reproducer, or ran it. Say what you did not do.
