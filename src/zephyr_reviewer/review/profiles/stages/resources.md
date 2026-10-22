# Stage: resources and memory

You account for every resource the change acquires or changes.

Look for:
- Allocations (`k_malloc`, `k_heap_alloc`, slabs, `net_buf`, `net_pkt`) not released on error paths
  or on the success path; double release; release of the wrong object.
- Buffers sized from the wrong constant; copies without a bound; off-by-one on terminators;
  stack buffers too large for the thread stack.
- Reference counts and `net_buf` ownership transferred incorrectly.
- Objects initialized partially before being published (added to a list, registered, enabled).
- Device state left inconsistent after a failed init (clocks, pins, interrupts enabled with no
  handler, power states).
- Resources held across suspend/resume or deinit.

Follow each resource from acquisition to release along every path. Read the whole function and
the helpers it calls.
