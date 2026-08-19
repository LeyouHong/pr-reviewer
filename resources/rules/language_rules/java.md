### Context: java

The dominant defect classes in reviewed Java diffs are null dereference, boxed-type comparison, and resources that leak on the exception path. Each has a deterministic check; apply the check rather than reasoning from the type signature.

### Criteria: java-null

- An added dereference of a value returned by a method that a readable path can return `null` from MUST be flagged at `error` when the null-producing path is visible, and at `warning` when it is inferred.
- A `Boolean` wrapper used in a boolean context (`if (flag)`, `!flag`) MUST be flagged at `error`; auto-unboxing throws when the value is `null`.
- A map lookup whose result is dereferenced without a containment or null check MUST be flagged at `error`.

### Criteria: java-equality

- `==` or `!=` applied to `String`, `Integer`, `Long`, or any boxed type MUST be flagged at `error`. Values outside the integer cache compare by reference.
- `equals` implemented without a matching `hashCode`, or either implemented on a class used as a map key, MUST be flagged at `warning`.

### Criteria: java-resources

- A stream, reader, writer, socket, `Connection`, `Statement`, or `ResultSet` acquired outside try-with-resources, where an added path can throw before `close()`, MUST be flagged at `error`.
- An `ExecutorService` created and never shut down MUST be flagged at `error`. Each construction leaks its threads.
- A lock acquired where `unlock()` is not in a `finally` block MUST be flagged at `error`.

### Criteria: java-concurrency

- Shared mutable state accessed from multiple threads without synchronization, a concurrent collection, or a volatile marker MUST be flagged at `warning`.
- Check-then-act on a shared collection (`containsKey` followed by `put`) MUST be flagged at `warning`; the pair is not atomic.
- A non-thread-safe formatter or parser held in a static or shared field MUST be flagged at `error`.

### Criteria: java-collections

- Modification of a collection while iterating it MUST be flagged at `error`.
- A hard-coded index (`get(0)`) inside a loop whose body otherwise uses the loop variable MUST be flagged at `error`. This is the canonical copy-paste defect.

### Criteria: java-security

- String concatenation of caller-supplied data into a SQL statement, a command line, or a file path MUST be flagged at `error` under the security category.
- Deserialization of untrusted input MUST be flagged at `error`.

### Directive: java-evidence

- When flagging a null dereference, name three things: where the null originates, the path that carries it, and the dereference site. A signature that merely permits null is insufficient evidence.

### Directive: java-proportionality

- Proportional feedback: focus on correctness and maintainability over micro-optimizations.
- Unit tests for regex and parsing changes should cover both the old and the new format during a transition.
- API misuse or race risks are flagged at `warning` unless a concrete failure scenario is shown in the diff. Performance tuning proposed without profiling evidence is flagged at `info`.
