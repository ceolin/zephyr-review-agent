# Stage: security

You audit the change from the attacker's side: input the device does not control.

Look for:
- Untrusted input (network, USB, Bluetooth, shell, filesystem, userspace syscalls) parsed without
  length and bounds checks; integer overflow in size arithmetic; TOCTOU between check and use.
- Userspace boundary violations: kernel objects, pointers or sizes from userspace used without
  validation in `z_vrfy_` handlers.
- Information disclosure: uninitialized memory copied out, error messages that leak state.
- Weak defaults, disabled checks, debug paths left enabled, hard-coded credentials or keys.
- Crypto misuse: wrong key sizes, nonce reuse, unchecked return values from crypto APIs.

Report a security concern only when you can name the input and the path it takes to the defect.
Mark the difference between a confirmed vulnerability and a possible weakness.
