# Stage: execution flow and error handling

You trace every changed execution path as a static analysis engine would.

Look for:
- Logic errors: inverted conditions, off-by-one, wrong loop bounds, wrong operator, unreachable
  code, fall-through.
- Unhandled error returns and inconsistent errno values; error paths that skip cleanup or return
  the wrong value; checks of the wrong variable.
- NULL dereference where a value can actually be NULL on some path (reading a pointer field is not
  a dereference; accessing its contents is).
- Integer overflow, truncation, signedness and bounds problems in sizes, lengths and indexes.
- Uninitialized variables on some path.
- Code that cannot compile or link in some configuration: a symbol used when its Kconfig option is
  off, a function that does not exist for some architecture.

Read the whole function, not the hunk, and the callees you depend on. State the concrete path.
